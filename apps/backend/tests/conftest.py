import pytest

from app.core.local_access import require_local_access
from app.features.workflows import runners
from app.main import app


@pytest.fixture(autouse=True)
def cdc_settings_present(request, monkeypatch):
    """The check itself is tested in test_cdc_settings_check.py; other tests are about what runs after it."""
    if not request.module.__name__.endswith("test_cdc_settings_check"):
        monkeypatch.setattr(runners, "_check_cdc_settings", lambda: None)


@pytest.fixture(autouse=True)
def local_access_open(request):
    """Access itself is tested in test_local_access.py; other API tests are about what is behind it."""
    if request.module.__name__.endswith("test_local_access"):
        yield
        return
    app.dependency_overrides[require_local_access] = lambda: None
    yield
    app.dependency_overrides.pop(require_local_access, None)
