import stat

import pytest
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core import local_access
from app.core.local_access import ensure_api_token, require_local_access
from app.main import app as real_app

TOKEN = "a-fixed-test-token"


def make_client(client_host="127.0.0.1", host="localhost") -> TestClient:
    app = FastAPI()

    @app.get("/x", dependencies=[Depends(require_local_access)])
    def x() -> dict:
        return {"ok": True}

    return TestClient(app, client=(client_host, 50000), base_url=f"http://{host}")


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ALPHA_API_TOKEN", TOKEN)
    monkeypatch.delenv("ALPHA_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(local_access, "_token", None)
    return tmp_path


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_right_token_from_this_pc_passes():
    assert make_client().get("/x", headers=auth()).status_code == 200
    assert make_client(host="127.0.0.1:8000").get("/x", headers=auth()).status_code == 200
    assert make_client(client_host="::1", host="[::1]:8000").get("/x", headers=auth()).status_code == 200


def test_no_or_wrong_token_is_refused():
    assert make_client().get("/x").status_code == 401
    assert make_client().get("/x", headers=auth("wrong")).status_code == 401
    assert make_client().get("/x", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401


def test_another_machine_is_refused_even_with_the_token():
    assert make_client(client_host="192.168.0.5").get("/x", headers=auth()).status_code == 403


def test_a_page_that_resolves_its_name_to_this_pc_is_refused():
    assert make_client(host="evil.example").get("/x", headers=auth()).status_code == 403


def test_allowed_hosts_can_be_set(monkeypatch):
    monkeypatch.setenv("ALPHA_ALLOWED_HOSTS", "alpha.localhost")
    assert make_client(host="alpha.localhost:8000").get("/x", headers=auth()).status_code == 200
    assert make_client().get("/x", headers=auth()).status_code == 403


def test_a_token_is_made_once_per_launch_with_owner_only_access(monkeypatch, env):
    monkeypatch.delenv("ALPHA_API_TOKEN")

    token = ensure_api_token()
    path = env / local_access.TOKEN_FILE

    assert path.read_text() == token and ensure_api_token() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_all_migration_routes_are_behind_it():
    client = TestClient(real_app, client=("127.0.0.1", 50000), base_url="http://localhost")
    for method, url in (("GET", "/db-migration/status"), ("POST", "/db-migration/runs"),
                        ("GET", "/db-migration/runs/mig-1"), ("POST", "/db-migration/runs/mig-1/finish"),
                        ("GET", "/db-migration/secrets/mig-1"), ("PUT", "/db-migration/secrets/mig-1/source-cloud")):
        assert client.request(method, url).status_code == 401, url
    assert client.get("/health").status_code == 200
