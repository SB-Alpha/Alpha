"""Proxy ingress check (#34). The database is mocked; these tests need no PostgreSQL."""

import logging
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

SECRET = "test-ingress-secret"


@pytest.fixture(autouse=True)
def mock_db():
    with patch("app.db.open_pool"), patch("app.db.apply_schema"), patch("app.db.close_pool"):
        yield


@pytest.fixture
def client():
    with patch.dict(os.environ, {"WORKER_SECRET": SECRET}, clear=True), TestClient(app) as test_client:
        yield test_client


def test_without_a_secret_every_route_stays_open():
    with patch.dict(os.environ, {}, clear=True), TestClient(app) as open_client:
        assert open_client.get("/").status_code == 200
        assert open_client.get("/health/live").status_code == 200


def test_health_stays_open_for_platform_checks(client):
    res = client.get("/health/live")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/", "/docs", "/openapi.json"])
def test_direct_access_without_the_header_is_rejected(client, path):
    res = client.get(path)
    assert res.status_code == 403
    assert "Direct access forbidden" in res.json()["detail"]


@pytest.mark.parametrize("value", ["wrong-secret", "", SECRET + "x", "한글".encode()])
def test_a_wrong_header_is_rejected(client, value):
    assert client.get("/", headers={"X-Alpha-Worker-Secret": value}).status_code == 403


@pytest.mark.parametrize("name", ["X-Alpha-Worker-Secret", "x-alpha-worker-secret"])
def test_the_worker_secret_is_accepted(client, name):
    assert client.get("/", headers={name: SECRET}).status_code == 200


def test_a_blocked_request_is_still_logged(client, caplog):
    with caplog.at_level(logging.INFO, logger="target-app"):
        res = client.get("/")
    assert res.status_code == 403
    assert res.headers.get("X-Request-Id")
    assert any('"status": 403' in line for line in caplog.messages)
