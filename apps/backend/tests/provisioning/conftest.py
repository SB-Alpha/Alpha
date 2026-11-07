import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def isolated_secret_store(tmp_path, monkeypatch):
    """provisioning 이 목적지 비밀을 저장해도 개발자 PC 의 실제 저장소와 키체인을 건드리지 않게 한다."""
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path / "alpha-data"))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())
