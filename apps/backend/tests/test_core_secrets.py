import stat

import pytest
from cryptography.fernet import Fernet

from app.core import secrets


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())
    return tmp_path


def test_put_returns_a_reference_and_resolve_gives_the_value():
    ref = secrets.put_secret("mig-001/source-db-admin", {"user": "alpha", "password": "pw"})

    assert ref == "secret://mig-001/source-db-admin"
    assert secrets.resolve(ref) == {"user": "alpha", "password": "pw"}


def test_same_name_overwrites():
    secrets.put_secret("mig-001/a", {"v": "1"})
    secrets.put_secret("secret://mig-001/a", {"v": "2"})

    assert secrets.resolve("secret://mig-001/a") == {"v": "2"}


def test_missing_raises_key_error_and_delete_removes():
    secrets.put_secret("mig-001/a", {"v": "1"})
    secrets.delete_secret("secret://mig-001/a")

    with pytest.raises(KeyError):
        secrets.resolve("secret://mig-001/a")
    secrets.delete_secret("secret://mig-001/a")  # already gone is fine


def test_value_is_encrypted_in_the_file_and_only_the_owner_can_read_it(store):
    secrets.put_secret("mig-001/a", {"password": "plain-text-marker"})
    path = store / "alpha.db"

    assert b"plain-text-marker" not in path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_wrong_key_is_an_error_without_the_value(monkeypatch):
    ref = secrets.put_secret("mig-001/a", {"password": "plain-text-marker"})
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())

    with pytest.raises(RuntimeError) as exc:
        secrets.resolve(ref)
    assert "plain-text-marker" not in str(exc.value)


def test_key_comes_from_the_keychain_and_is_created_once(monkeypatch):
    saved = {}

    class Keyring:
        @staticmethod
        def get_password(service, user):
            return saved.get((service, user))

        @staticmethod
        def set_password(service, user, value):
            saved[(service, user)] = value

    monkeypatch.delenv("ALPHA_SECRETS_KEY")
    monkeypatch.setitem(__import__("sys").modules, "keyring", Keyring)
    ref = secrets.put_secret("mig-001/a", {"v": "1"})

    assert secrets.resolve(ref) == {"v": "1"}
    assert len(saved) == 1
