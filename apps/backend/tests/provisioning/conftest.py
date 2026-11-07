import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def isolated_secret_store(tmp_path, monkeypatch):
    """provisioning 이 목적지 비밀을 저장해도 개발자 PC 의 실제 저장소와 키체인을 건드리지 않게 한다."""
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path / "alpha-data"))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def no_real_ingress_probe(monkeypatch):
    """deploy_app 이 배포한 앱 주소를 실제로 치지 않게 한다 (0 = 연결 실패, 판단하지 않음). 프로브를 검사하는 테스트는 직접 넘긴다."""
    from app.features.provisioning import service

    monkeypatch.setattr(service, "_default_ingress_probe", lambda url, secret: 0)
