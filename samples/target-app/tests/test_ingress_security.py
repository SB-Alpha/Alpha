from unittest.mock import patch
import os
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def mock_db():
    with patch("app.db.open_pool"), patch("app.db.apply_schema"), patch("app.db.close_pool"):
        yield


def test_ingress_no_secret_configured():
    with patch.dict(os.environ, {}, clear=True), patch("app.main.WORKER_SECRET", None):
        with TestClient(app) as client:
            res = client.get("/health/live")
            assert res.status_code == 200
            assert res.json() == {"status": "ok"}


def test_ingress_health_bypasses_secret():
    with patch.dict(os.environ, {"WORKER_SECRET": "test-ingress-secret"}, clear=True), patch("app.main.WORKER_SECRET", "test-ingress-secret"):
        with TestClient(app) as client:
            # /health/live does not require X-Alpha-Worker-Secret
            res = client.get("/health/live")
            assert res.status_code == 200
            assert res.json() == {"status": "ok"}


def test_ingress_blocks_unauthorized_direct_access():
    with patch.dict(os.environ, {"WORKER_SECRET": "test-ingress-secret"}, clear=True), patch("app.main.WORKER_SECRET", "test-ingress-secret"):
        with TestClient(app) as client:
            # Request to protected route without header is rejected
            res = client.get("/")
            assert res.status_code == 403
            assert "Direct access forbidden" in res.json().get("detail", "")

            # Request with invalid header is rejected
            res_wrong = client.get("/", headers={"X-Alpha-Worker-Secret": "wrong-secret"})
            assert res_wrong.status_code == 403


def test_ingress_allows_valid_worker_secret():
    with patch.dict(os.environ, {"WORKER_SECRET": "test-ingress-secret"}, clear=True), patch("app.main.WORKER_SECRET", "test-ingress-secret"):
        with TestClient(app) as client:
            # Root route serves index.html if file exists, or passes proxy secret middleware
            res = client.get("/", headers={"X-Alpha-Worker-Secret": "test-ingress-secret"})
            assert res.status_code != 403

            # Case-insensitivity check (e.g., lower-case header from HTTP/2)
            res_lower = client.get("/", headers={"x-alpha-worker-secret": "test-ingress-secret"})
            assert res_lower.status_code != 403
