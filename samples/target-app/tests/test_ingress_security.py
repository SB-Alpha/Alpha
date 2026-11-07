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


def test_without_a_secret_in_local_mode_routes_stay_open():
    with patch.dict(os.environ, {"APP_ENV": "local"}, clear=True), TestClient(app) as open_client:
        assert open_client.get("/").status_code == 200
        assert open_client.get("/health/live").status_code == 200


def test_aws_source_without_require_switch_stays_open():
    # AWS 원본(APP_ENV=aws)에서는 REQUIRE_WORKER_SECRET 스위치가 없으면 비인가 차단 없이 정상 통과 (200)
    with patch.dict(os.environ, {"APP_ENV": "aws"}, clear=True), TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/health/live").status_code == 200


def test_aws_source_without_require_switch_serves_the_api():
    # 원본의 업무 API가 열려 있는지 확인 (DB 결과는 빈 목록으로 대체)
    with patch.dict(os.environ, {"APP_ENV": "aws"}, clear=True), patch("app.db.pool") as pool, TestClient(app) as client:
        pool.connection.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
        res = client.get("/api/work-items")
    assert res.status_code == 200
    assert res.json() == []


def test_require_worker_secret_with_the_secret_accepts_the_worker():
    env = {"APP_ENV": "gcp", "REQUIRE_WORKER_SECRET": "true", "WORKER_SECRET": SECRET}
    with patch.dict(os.environ, env, clear=True), TestClient(app) as client:
        assert client.get("/").status_code == 403
        assert client.get("/", headers={"X-Alpha-Worker-Secret": SECRET}).status_code == 200


def test_require_worker_secret_without_secret_is_blocked_fail_closed():
    # REQUIRE_WORKER_SECRET=true가 켜진 대상 환경(GCP 등)에서 시크릿이 누락되면 403으로 즉시 차단
    with patch.dict(os.environ, {"APP_ENV": "gcp", "REQUIRE_WORKER_SECRET": "true"}, clear=True), TestClient(app) as client:
        res = client.get("/")
        assert res.status_code == 403
        assert "WORKER_SECRET is required" in res.json()["detail"]


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
