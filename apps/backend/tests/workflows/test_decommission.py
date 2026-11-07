"""전환이 끝난 run 의 이전 자원 정리 (복제 작업 종료, VPN 삭제).

전환에 성공한 run 은 지금까지 db_workflow.finish 를 부르는 곳이 없어 복제 슬롯·CDC 작업·VPN 이 남았습니다.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.features.workflows import runners
from app.features.workflows.engine import Engine
from app.features.workflows.router import get_engine
from app.features.workflows.schemas import StageName, WorkflowState
from app.features.workflows.store import RunStore
from app.main import app
from tests.workflows.test_api import BODY


def _succeeded_run(http: TestClient) -> str:
    run_id = http.post("/runs", json=BODY).json()["run_id"]
    for stage in ("discovery", "planning"):
        http.post(f"/runs/{run_id}/stages/{stage}/manual", json={"output": {"stage": stage}})
    http.post(f"/runs/{run_id}/approve", json={"gate": "plan"})
    for stage in ("provisioning", "db_migration", "app_deploy"):
        http.post(f"/runs/{run_id}/stages/{stage}/manual", json={"output": {"stage": stage}})
    http.post(f"/runs/{run_id}/approve", json={"gate": "cutover", "acknowledge_unknown": True})
    http.post(f"/runs/{run_id}/stages/cutover/manual", json={"output": {"serving": "gcp"}})
    assert http.get(f"/runs/{run_id}").json()["state"] == "succeeded"
    return run_id


@pytest.fixture
def http_with(tmp_path):
    def make(decommission):
        engine = Engine(RunStore(tmp_path / "workflows.sqlite3"), decommission=decommission)
        app.dependency_overrides[get_engine] = lambda: engine
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


def test_a_succeeded_run_can_be_decommissioned_and_a_failure_retried(http_with):
    attempts = []

    def decommission(run) -> str:
        attempts.append(run.run_id)
        if len(attempts) == 1:
            raise RuntimeError("남은 항목: vpn")
        return "이전 자원 정리 완료"

    http = http_with(decommission)
    run_id = http.post("/runs", json=BODY).json()["run_id"]
    assert http.post(f"/runs/{run_id}/decommission").status_code == 409  # 전환이 끝나기 전

    run_id = _succeeded_run(http)
    assert http.post(f"/runs/{run_id}/decommission").json()["cleanup_state"] == "running"
    run = http.get(f"/runs/{run_id}").json()  # TestClient 는 백그라운드 작업까지 끝낸 상태
    assert (run["cleanup_state"], run["state"]) == ("failed", "succeeded")  # 정리 실패가 이전 결과를 바꾸지 않음
    assert "vpn" in run["cleanup_message"]

    assert http.post(f"/runs/{run_id}/decommission").json()["cleanup_state"] == "running"
    run = http.get(f"/runs/{run_id}").json()
    assert (run["cleanup_state"], run["cleanup_message"]) == ("done", "이전 자원 정리 완료")
    assert http.post(f"/runs/{run_id}/decommission").status_code == 409  # 이미 끝남
    assert attempts == [run_id, run_id]


def test_decommission_is_refused_when_nothing_is_wired(http_with):
    http = http_with(None)
    run_id = _succeeded_run(http)
    assert http.post(f"/runs/{run_id}/decommission").status_code == 409


def test_decommission_is_refused_when_verification_fails(http_with):
    http = http_with(lambda r: "정리 완료")
    run_id = http.post("/runs", json=BODY).json()["run_id"]
    for stage in ("discovery", "planning"):
        http.post(f"/runs/{run_id}/stages/{stage}/manual", json={"output": {"stage": stage}})
    http.post(f"/runs/{run_id}/approve", json={"gate": "plan"})
    for stage in ("provisioning", "db_migration", "app_deploy"):
        http.post(f"/runs/{run_id}/stages/{stage}/manual", json={"output": {"stage": stage}})
    http.post(f"/runs/{run_id}/approve", json={"gate": "cutover", "acknowledge_unknown": True})
    # 검증 실패(mismatch)가 기록된 경우
    http.post(f"/runs/{run_id}/stages/cutover/manual", json={"output": {"serving": "gcp", "verification": {"status": "mismatch"}}})

    res = http.post(f"/runs/{run_id}/decommission")
    assert res.status_code == 409
    assert "match" in res.json()["detail"]


def test_decommission_records_operator(http_with):
    http = http_with(lambda r: "정리 완료")
    run_id = _succeeded_run(http)
    res = http.post(f"/runs/{run_id}/decommission?operator=admin-pj")
    assert res.status_code == 200
    assert "admin-pj" in res.json()["cleanup_message"]


def test_decommission_is_refused_when_worker_is_not_target(http_with, monkeypatch):
    from app.features.provisioning.worker_client import WorkerClient

    class FakeWorker:
        def status(self):
            return {"mode": "SOURCE"}

    monkeypatch.setattr(WorkerClient, "from_env", lambda: FakeWorker())
    http = http_with(lambda r: "정리 완료")
    run_id = _succeeded_run(http)
    res = http.post(f"/runs/{run_id}/decommission")
    assert res.status_code == 409
    assert "TARGET" in res.json()["detail"]


# -- runners.decommission_cdc: db_workflow.finish 결과를 문장으로 --------------------------------------


def _run(manual: bool = False, state: WorkflowState = WorkflowState.SUCCEEDED):
    stage = SimpleNamespace(manual=manual, state=state)
    return SimpleNamespace(run_id="r-1", stage=lambda name: stage if name == StageName.DB_MIGRATION else None)


def _finish(**fields):
    base = {"state": "finished", "vpn": "removed", "remaining": [], "next_steps": [], "message": None}
    return SimpleNamespace(**{**base, **fields})


@pytest.fixture
def agent_env(monkeypatch):
    monkeypatch.setenv("CDC_AGENT_URL", "http://agent.test")
    monkeypatch.setenv("CDC_AGENT_TOKEN", "t")


def test_manual_db_stage_has_nothing_to_decommission():
    assert "수동" in runners.decommission_cdc(_run(manual=True))


def test_finish_result_becomes_the_done_message(monkeypatch, agent_env):
    from app.features.db_migration import db_workflow

    calls = []
    monkeypatch.setattr(db_workflow, "finish",
                        lambda run_id, agent: calls.append(run_id) or _finish(next_steps=["IAM 사용자를 삭제하세요."]))
    message = runners.decommission_cdc(_run())
    assert calls == ["r-1"]
    assert "VPN 삭제" in message and "IAM 사용자를 삭제하세요." in message


def test_an_incomplete_finish_raises_with_what_is_left(monkeypatch, agent_env):
    from app.features.db_migration import db_workflow

    monkeypatch.setattr(db_workflow, "finish",
                        lambda run_id, agent: _finish(state="incomplete", vpn="kept", remaining=["vpn", "agent_job"],
                                                      message="destroy timed out"))
    with pytest.raises(RuntimeError, match="vpn, agent_job"):
        runners.decommission_cdc(_run())


def test_without_an_agent_url_it_fails_loudly(monkeypatch):
    monkeypatch.delenv("CDC_AGENT_URL", raising=False)
    monkeypatch.delenv("ALPHA_CDC_SIMULATION", raising=False)  # 시뮬레이션이면 mock 에이전트로 갑니다
    with pytest.raises(RuntimeError, match="CDC_AGENT_URL"):
        runners.decommission_cdc(_run())
