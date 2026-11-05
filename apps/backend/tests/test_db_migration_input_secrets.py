import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.secrets import has_secret, put_secret, resolve
from app.main import app

URL = "/db-migration/secrets/mig-001"
ADMIN = {"user": "alpha", "password": "plain-text-marker"}


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())
    return tmp_path


client = TestClient(app)


def test_save_returns_only_the_reference_and_run_can_resolve_it():
    resp = client.put(f"{URL}/source-db-admin", json=ADMIN)

    assert resp.status_code == 200
    assert resp.json() == {"ref": "secret://mig-001/source-db-admin"}
    assert resolve(resp.json()["ref"]) == ADMIN


def test_value_is_encrypted_in_the_file(store):
    client.put(f"{URL}/source-db-admin", json=ADMIN)

    assert b"plain-text-marker" not in (store / "alpha.db").read_bytes()


def test_status_says_what_is_saved_without_values():
    client.put(f"{URL}/source-db-admin", json=ADMIN)

    resp = client.get(URL)

    assert resp.json()["source-db-admin"] == {"ref": "secret://mig-001/source-db-admin", "saved": True}
    assert resp.json()["target-cloud"]["saved"] is False
    assert "plain-text-marker" not in resp.text


def test_cloud_keys_take_an_optional_session_token():
    value = {"aws_access_key_id": "a", "aws_secret_access_key": "b", "aws_session_token": "c"}

    assert client.put(f"{URL}/source-cloud", json=value).status_code == 200
    assert client.put(f"{URL}/target-cloud", json={"credentials_json": "{}"}).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"user": "alpha"},
        {"user": "alpha", "password": ""},
        {"user": "alpha", "password": 1},
        {"user": "alpha", "password": "x", "plain-text-marker": "y"},
        ["plain-text-marker"],
    ],
)
def test_bad_body_is_refused_without_quoting_it(body):
    resp = client.put(f"{URL}/source-db-admin", json=body)

    assert resp.status_code == 422
    assert "plain-text-marker" not in resp.text
    assert not has_secret("secret://mig-001/source-db-admin")


def test_broken_json_is_refused_without_quoting_it():
    resp = client.put(f"{URL}/source-db-admin", content=b"{plain-text-marker")

    assert resp.status_code == 422
    assert "plain-text-marker" not in resp.text


def test_secrets_this_module_makes_cannot_be_overwritten_or_removed():
    put_secret("secret://mig-001/vpn-psk", {"gcp-0": "keep"})

    assert client.put(f"{URL}/vpn-psk", json={"gcp-0": "x"}).status_code == 404
    assert client.delete(f"{URL}/vpn-psk").status_code == 404
    assert resolve("secret://mig-001/vpn-psk") == {"gcp-0": "keep"}


def test_run_id_follows_the_run_rule():
    assert client.put("/db-migration/secrets/Bad_Id/source-db-admin", json=ADMIN).status_code == 422


def test_delete_removes_and_can_be_repeated():
    client.put(f"{URL}/source-db-admin", json=ADMIN)

    assert client.delete(f"{URL}/source-db-admin").status_code == 204
    assert client.delete(f"{URL}/source-db-admin").status_code == 204
    assert not has_secret("secret://mig-001/source-db-admin")
