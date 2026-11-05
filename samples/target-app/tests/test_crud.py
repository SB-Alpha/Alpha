"""CRUD tests against a real PostgreSQL (start it with `docker compose up -d db`).

Connection settings come from the PG* environment variables.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    assert client.get("/health/live").json() == {"status": "ok"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "db": "ok"}


def test_crud_round_trip(client):
    created = client.post("/api/work-items", json={"title": "test item", "description": "d"})
    assert created.status_code == 201
    item = created.json()
    assert uuid.UUID(item["id"])
    assert item["status"] == "todo"

    fetched = client.get(f"/api/work-items/{item['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "test item"

    updated = client.patch(f"/api/work-items/{item['id']}", json={"status": "done"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "done"
    assert updated.json()["title"] == "test item"
    assert updated.json()["updated_at"] >= item["updated_at"]

    listed = client.get("/api/work-items", params={"limit": 200})
    assert listed.status_code == 200
    assert any(row["id"] == item["id"] for row in listed.json())

    assert client.delete(f"/api/work-items/{item['id']}").status_code == 204
    assert client.get(f"/api/work-items/{item['id']}").status_code == 404
    assert client.delete(f"/api/work-items/{item['id']}").status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/work-items", {"title": ""}),
        ("post", "/api/work-items", {"title": "x", "status": "bogus"}),
        ("post", "/api/work-items", {"title": "a\u0000b"}),
        ("get", "/api/work-items/not-a-uuid", None),
        ("patch", f"/api/work-items/{uuid.uuid4()}", {}),
    ],
)
def test_invalid_input_is_4xx(client, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    response = getattr(client, method)(path, **kwargs)
    assert 400 <= response.status_code < 500


def test_response_headers(client):
    response = client.get("/health/live", headers={"X-Request-Id": "abc"})
    assert response.headers["X-Request-Id"] == "abc"
    assert response.headers["X-Served-By"]
