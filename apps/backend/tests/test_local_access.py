import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.local_access import require_local_access
from app.main import app as real_app

SCREEN = "http://localhost:3000"


def make_client(client_host="127.0.0.1", host="localhost") -> TestClient:
    app = FastAPI()

    @app.post("/x", dependencies=[Depends(require_local_access)])
    def x() -> dict:
        return {"ok": True}

    return TestClient(app, client=(client_host, 50000), base_url=f"http://{host}")


def origin(value=SCREEN):
    return {"Origin": value}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.delenv("ALPHA_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("ALPHA_ALLOWED_ORIGINS", raising=False)


def test_the_screen_on_this_pc_passes():
    assert make_client().post("/x", headers=origin()).status_code == 200
    assert make_client(host="127.0.0.1:8000").post("/x", headers=origin("http://127.0.0.1:3000")).status_code == 200
    assert make_client(client_host="::1", host="[::1]:8000").post("/x", headers=origin("http://[::1]:3000")).status_code == 200


def test_a_program_on_this_pc_sends_no_origin_and_passes():
    assert make_client().post("/x").status_code == 200


@pytest.mark.parametrize("value", [
    "https://evil.example",
    "null",  # sandboxed frames and local files
    "",
    "http://localhost:3001",
    "https://localhost:3000",
    "http://localhost:3000.evil.example",
])
def test_another_page_is_refused(value):
    response = make_client().post("/x", headers=origin(value))
    assert response.status_code == 403 and "Origin" in response.json()["detail"]


def test_another_machine_is_refused():
    assert make_client(client_host="192.168.0.5").post("/x", headers=origin()).status_code == 403


def test_a_page_that_resolves_its_name_to_this_pc_is_refused():
    assert make_client(host="evil.example").post("/x").status_code == 403


def test_allowed_hosts_can_be_set(monkeypatch):
    monkeypatch.setenv("ALPHA_ALLOWED_HOSTS", "alpha.localhost")
    assert make_client(host="alpha.localhost:8000").post("/x").status_code == 200
    assert make_client().post("/x").status_code == 403


def test_allowed_origins_can_be_set(monkeypatch):
    monkeypatch.setenv("ALPHA_ALLOWED_ORIGINS", "http://localhost:4000, app://alpha")
    assert make_client().post("/x", headers=origin("http://localhost:4000")).status_code == 200
    assert make_client().post("/x", headers=origin("app://alpha")).status_code == 200
    assert make_client().post("/x", headers=origin()).status_code == 403


def test_all_migration_routes_are_behind_it():
    client = TestClient(real_app, client=("127.0.0.1", 50000), base_url="http://localhost")
    for method, url in (("GET", "/db-migration/status"), ("POST", "/db-migration/runs"),
                        ("GET", "/db-migration/runs/mig-1"), ("POST", "/db-migration/runs/mig-1/stop"),
                        ("POST", "/db-migration/runs/mig-1/finish"), ("POST", "/db-migration/jobs/mig_1/abort"),
                        ("GET", "/db-migration/secrets/mig-1"), ("PUT", "/db-migration/secrets/mig-1/source-cloud")):
        assert client.request(method, url, headers=origin("https://evil.example")).status_code == 403, url
    assert client.get("/health", headers=origin("https://evil.example")).status_code == 200
