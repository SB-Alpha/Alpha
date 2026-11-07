"""새 run 입구에서 실제 조회 모듈과 실행 모델 변환까지 연결한다."""

import threading
import time
from itertools import permutations

import pytest

from app.core.secrets import has_secret
from app.features.db_migration import cloud_lookup, db_workflow
from app.features.db_migration.client import AgentError
from app.features.db_migration.start_checks import StartCheckInput

from .test_cloud_lookup import REGIONS, SECRET, request_input, world  # noqa: F401 - 공통 API fixture


class NoJobAgent:
    def get_job(self, job_id):
        raise AgentError(404, "not found")

    def abort(self, job_id):
        raise AgentError(404, "not found")


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.005)
    raise AssertionError("background lookup did not finish")


@pytest.fixture(autouse=True)
def run_state(world, monkeypatch):
    monkeypatch.setenv("CDC_COMPLETE_POLL_S", "0.01")
    with db_workflow._active_lock:
        db_workflow._active.clear()
        db_workflow._runs.clear()
        db_workflow._cleaning.clear()
        db_workflow._stop_requested.clear()
    yield
    wait_until(lambda: not db_workflow._active)
    wait_until(lambda: not db_workflow._cleaning)


@pytest.mark.parametrize("source,target", tuple(permutations(REGIONS, 2)))
def test_run_reaches_lookup_on_all_six_routes(world, source, target):
    agent = NoJobAgent()
    run_id = db_workflow.run(request_input(source, target), agent=agent)
    wait_until(lambda: run_id not in db_workflow._active)
    found = db_workflow._runs[run_id].lookup
    assert (found.source.cloud, found.target.cloud) == (source, target)
    execution = db_workflow._runs[run_id].execution
    assert execution.run_id == run_id
    assert (execution.source.cloud, execution.target.cloud) == (source, target)
    assert execution.source.db.host == found.source.db.host
    assert execution.target.db.host == found.target.db.host
    assert execution.source.credentials_ref.cloud == f"secret://cloud/{source}"
    assert execution.target.credentials_ref.cloud == f"secret://cloud/{target}"
    assert StartCheckInput.from_route(execution).source_database is None
    status = db_workflow.status(run_id, agent=agent)
    # 조회·변환까지 끝나고 다음 단계인 시작 전 검사에서 멈춘다. 뒤 단계는 test_run_flow.py가 본다.
    assert status.state == "failed" and "CDC_OCI_VCN_CIDR" in status.message
    assert status.metrics["target_db_network"] == "PRIVATE"
    assert not status.ready_for_cutover
    assert SECRET not in status.model_dump_json()


def test_api_permission_failure_is_recorded_with_cleanup_remaining(world):
    world["errors"]["/v1/projects/db-project/instances"] = 403
    agent = NoJobAgent()
    run_id = db_workflow.run(request_input(), agent=agent)
    wait_until(lambda: run_id not in db_workflow._active)
    status = db_workflow.status(run_id, agent=agent)
    assert status.state == "failed" and "target (gcp)" in status.message and "403" in status.message
    assert db_workflow._runs[run_id].lookup is None
    assert db_workflow._runs[run_id].execution is None
    assert status.cleanup_required and has_secret(f"secret://{run_id}/source-db-admin")
    assert SECRET not in status.model_dump_json()


def test_run_returns_without_waiting_for_cloud_and_reserves_run(world, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    find_route = cloud_lookup.find_route

    def blocked(inp):
        entered.set()
        assert release.wait(5)
        return find_route(inp)

    monkeypatch.setattr(cloud_lookup, "find_route", blocked)
    agent = NoJobAgent()
    try:
        run_id = db_workflow.run(request_input(), agent=agent)
        assert entered.wait(2)
        assert db_workflow.status(run_id, agent=agent).state == "running"
        with pytest.raises(db_workflow.NotAllowed):
            db_workflow.run(request_input(), agent=agent)
    finally:
        release.set()
    wait_until(lambda: not db_workflow._active)


def test_stop_during_lookup_prevents_handoff(world, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    find_route = cloud_lookup.find_route

    def blocked(inp):
        entered.set()
        assert release.wait(5)
        return find_route(inp)

    monkeypatch.setattr(cloud_lookup, "find_route", blocked)
    agent = NoJobAgent()
    run_id = db_workflow.run(request_input(), agent=agent)
    assert entered.wait(2)
    results = []
    stopper = threading.Thread(target=lambda: results.append(db_workflow.stop(run_id, agent=agent)))
    try:
        stopper.start()
        wait_until(lambda: run_id in db_workflow._stop_requested)
    finally:
        release.set()
        stopper.join(5)
    assert not stopper.is_alive() and results
    assert db_workflow._runs[run_id].state == "aborted"
    assert db_workflow._runs[run_id].lookup is None
    assert db_workflow._runs[run_id].execution is None
    assert not has_secret(f"secret://{run_id}/source-db-admin")


def test_unexpected_background_error_is_sanitized(world, monkeypatch, caplog):
    def broken(inp):
        raise ValueError(SECRET)
    monkeypatch.setattr(cloud_lookup, "find_route", broken)
    agent = NoJobAgent()
    run_id = db_workflow.run(request_input(), agent=agent)
    wait_until(lambda: not db_workflow._active)
    status = db_workflow.status(run_id, agent=agent)
    assert status.state == "failed" and "조회를 완료하지 못했습니다" in status.message
    assert SECRET not in status.model_dump_json() + caplog.text


def test_thread_start_failure_releases_reservation(world, monkeypatch):
    def broken_start(self):
        raise RuntimeError("thread unavailable")
    monkeypatch.setattr(threading.Thread, "start", broken_start)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        db_workflow.run(request_input(), agent=NoJobAgent())
    assert not db_workflow._active
    assert db_workflow._runs["mig-lookup"].state == "failed"


def test_conversion_rejects_mismatched_lookup_without_partial_execution(world, monkeypatch):
    from dataclasses import replace
    find_route = cloud_lookup.find_route

    def mismatched(inp):
        found = find_route(inp)
        return replace(found, target=replace(found.target, region="another-region"))

    monkeypatch.setattr(cloud_lookup, "find_route", mismatched)
    agent = NoJobAgent()
    run_id = db_workflow.run(request_input(), agent=agent)
    wait_until(lambda: not db_workflow._active)
    status = db_workflow.status(run_id, agent=agent)
    assert status.state == "failed" and "target: lookup does not match" in status.message
    assert db_workflow._runs[run_id].lookup is not None
    assert db_workflow._runs[run_id].execution is None
    assert status.cleanup_required


def test_unexpected_conversion_error_is_sanitized(world, monkeypatch, caplog):
    def broken(*args):
        raise ValueError(SECRET)

    monkeypatch.setattr(db_workflow, "build_execution_model", broken)
    agent = NoJobAgent()
    run_id = db_workflow.run(request_input(), agent=agent)
    wait_until(lambda: not db_workflow._active)
    status = db_workflow.status(run_id, agent=agent)
    assert status.state == "failed" and "실행 모델로 변환하지 못했습니다" in status.message
    assert db_workflow._runs[run_id].execution is None
    assert SECRET not in status.model_dump_json() + caplog.text


def test_stop_during_conversion_prevents_execution_handoff(world, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    build = db_workflow.build_execution_model

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return build(*args)

    monkeypatch.setattr(db_workflow, "build_execution_model", blocked)
    agent = NoJobAgent()
    run_id = db_workflow.run(request_input(), agent=agent)
    assert entered.wait(2)
    results = []
    stopper = threading.Thread(target=lambda: results.append(db_workflow.stop(run_id, agent=agent)))
    try:
        stopper.start()
        wait_until(lambda: run_id in db_workflow._stop_requested)
    finally:
        release.set()
        stopper.join(5)
    assert not stopper.is_alive() and results
    assert db_workflow._runs[run_id].state == "aborted"
    assert db_workflow._runs[run_id].execution is None
    assert not has_secret(f"secret://{run_id}/source-db-admin")
