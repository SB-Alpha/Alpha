"""API rules for abort, secrets and probe, without Kafka."""

import socket

import pytest
from fastapi.testclient import TestClient

from cdc_agent import main
from cdc_agent.jobs import JobBusy
from cdc_agent.models import Job, JobRequest
from cdc_agent.secret_store import SecretsDisabled

REQUEST = {
    "job_id": "job01",
    "source": {"host": "src", "database": "postgres", "tables": ["public.work_items"], "app_role": "alpha"},
    "target": {"host": "tgt", "database": "app"},
}


@pytest.fixture
def client(monkeypatch):
    jobs: dict = {}

    class Store:
        def get(self, job_id):
            return jobs.get(job_id)

        def put(self, job):
            jobs[job.job_id] = job

    monkeypatch.setattr(main, "store", Store())
    c = TestClient(main.app)  # no context manager: lifespan would need Kafka
    c.jobs = jobs
    return c


def add(client, **fields):
    client.jobs["job01"] = Job(job_id="job01", request=JobRequest(**REQUEST), **fields)


@pytest.mark.parametrize("fields", [
    {"state": "aborted", "phase": "streaming"},
    {"state": "succeeded", "phase": "closed"},
    {"state": "running", "phase": "cutover"},
    {"state": "failed", "phase": "stabilizing"},
])
def test_abort_refused(client, fields):
    add(client, **fields)
    assert client.post("/jobs/job01/abort").status_code == 409


@pytest.mark.parametrize("fields", [
    {"state": "waiting_approval", "phase": "cutover_ready"},
    {"state": "failed", "phase": "streaming"},  # a failed setup can still hold a slot
    {"state": "pending", "phase": "source_prepare"},
])
def test_abort_cleans_up(client, monkeypatch, fields):
    add(client, **fields)
    monkeypatch.setattr(main.runner, "abort", lambda job_id: client.jobs[job_id])
    assert client.post("/jobs/job01/abort").status_code == 200


def test_abort_busy_and_failed_are_5xx_so_the_caller_keeps_the_vpn(client, monkeypatch):
    add(client, state="running", phase="streaming")

    def busy(job_id):
        raise JobBusy("busy")

    monkeypatch.setattr(main.runner, "abort", busy)
    assert client.post("/jobs/job01/abort").status_code == 503

    def broken(job_id):
        raise RuntimeError("unreachable")

    monkeypatch.setattr(main.runner, "abort", broken)
    assert client.post("/jobs/job01/abort").status_code == 502
    assert "정리 실패" in client.jobs["job01"].message


def test_retry_covers_source_prepare(client):
    add(client, state="failed", phase="source_prepare")
    assert client.post("/jobs/job01/retry").json()["state"] == "running"


def test_secrets_are_not_echoed(client, monkeypatch):
    stored = {}
    monkeypatch.setattr(main.secrets, "put", lambda job_id, values: stored.update(values))
    resp = client.put("/jobs/job01/secrets", json={"source.password": "hunter2"})

    assert resp.status_code == 200 and "hunter2" not in resp.text
    assert stored == {"source.password": "hunter2"}


def test_secrets_rules(client, monkeypatch):
    assert client.put("/jobs/..%2Fevil/secrets", json={}).status_code in (404, 422)

    def disabled(job_id, values):
        raise SecretsDisabled("no key")

    monkeypatch.setattr(main.secrets, "put", disabled)
    assert client.put("/jobs/job01/secrets", json={}).status_code == 503

    def bad(job_id, values):
        raise ValueError("missing keys")

    monkeypatch.setattr(main.secrets, "put", bad)
    assert client.put("/jobs/job01/secrets", json={}).status_code == 422


def test_probe_reports_each_endpoint(client):
    server = socket.create_server(("127.0.0.1", 0))
    open_port = server.getsockname()[1]
    closed = socket.create_server(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    result = client.post("/probe", json=[{"host": "127.0.0.1", "port": open_port},
                                         {"host": "127.0.0.1", "port": closed_port}]).json()
    server.close()

    assert [r["ok"] for r in result] == [True, False]
    assert result[1]["error"]
