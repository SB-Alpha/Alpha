"""영향 범위는 실제 경보를 run 토폴로지 노드에 맞춘 뒤에만 계산합니다. 고정 그래프·가짜 경보는 없습니다."""

import math
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.local_access import require_local_access
from app.features.security.router import get_security_service
from app.features.security.schemas import RerouteRequest, SecurityAlert, SourceStatus
from app.features.security.service import SecurityGovernanceService, match_node
from app.main import app

TOPOLOGY = {
    "nodes": [
        {"id": "source_app", "kind": "app", "label": "원본 앱", "cloud": "aws", "region": "ap-northeast-2",
         "resource": "target-app-task-service", "state": "succeeded"},
        {"id": "source_db", "kind": "database", "label": "원본 DB", "cloud": "aws", "region": "ap-northeast-2",
         "resource": "db-alpha-target", "state": "succeeded"},
        {"id": "cdc_agent", "kind": "cdc_agent", "label": "CDC 에이전트", "cloud": None, "region": None,
         "resource": None, "state": "running"},
        {"id": "target_db", "kind": "database", "label": "목적지 DB", "cloud": "gcp", "region": "asia-northeast3",
         "resource": "target-app-db", "state": "succeeded"},
        {"id": "target_app", "kind": "app", "label": "목적지 앱", "cloud": "gcp", "region": "asia-northeast3",
         "resource": "target-app", "state": "succeeded"},
    ],
    "edges": [
        {"id": "a", "from": "source_app", "to": "source_db", "kind": "app_db", "via": None},
        {"id": "b", "from": "source_db", "to": "cdc_agent", "kind": "replication", "via": "vpn"},
        {"id": "c", "from": "cdc_agent", "to": "target_db", "kind": "replication", "via": None},
        {"id": "d", "from": "target_app", "to": "target_db", "kind": "app_db", "via": None},
    ],
}

RDS_ALERT = SecurityAlert(
    id="aws:f1", provider="aws", severity="HIGH", resource_type="database", resource_id="db-alpha-target",
    threat_category="CredentialAccess:RDS/AnomalousBehavior.SuccessfulLogin", description="이상 로그인",
    detected_at=datetime(2026, 11, 8, tzinfo=timezone.utc), region="ap-northeast-2",
)
OTHER_ALERT = SecurityAlert(
    id="aws:f2", provider="aws", severity="LOW", resource_type="vm", resource_id="i-0123456789abcdef0",
    threat_category="Recon:EC2/PortProbeUnprotectedPort", description="다른 자원", region="ap-northeast-2",
)


def _service(alerts=(RDS_ALERT, OTHER_ALERT), topology=TOPOLOGY):
    seen_regions = []

    def fetch(regions):
        seen_regions.append(regions)
        status = SourceStatus(provider="aws", service="GuardDuty", status="ok", scope=",".join(regions),
                              count=len(alerts), checked_at=datetime.now(timezone.utc))
        return list(alerts), [status]

    service = SecurityGovernanceService(fetch=fetch)
    service.set_topology_provider(lambda run_id: topology if run_id == "mig-1" else None)
    return service, seen_regions


def test_feed_reads_guardduty_in_the_runs_aws_regions_only():
    service, seen = _service()
    feed = service.get_feed("mig-1")
    assert seen == [["ap-northeast-2"]]
    assert [a.id for a in feed.alerts] == ["aws:f1", "aws:f2"]


def test_blast_radius_matches_the_alert_to_the_real_node_and_decays_along_edges():
    service, _ = _service()
    result = service.calculate_blast_radius("mig-1", "aws:f1", depth_hops=2, alpha_decay=0.45)
    assert result.compromised_node_id == "source_db"
    assert result.max_risk_score == 0.8
    by_id = {n.node_id: n for n in result.impacted_nodes}
    assert by_id["source_db"].is_compromised and by_id["source_db"].hop_distance == 0
    assert by_id["source_app"].hop_distance == 1 and by_id["cdc_agent"].hop_distance == 1
    assert by_id["source_app"].risk_score == round(0.8 * math.exp(-0.45), 3)
    assert by_id["target_db"].hop_distance == 2
    assert "target_app" not in by_id  # 3 홉이라 depth 2 밖
    assert result.safe_reroute_targets == ["target_app"]


def test_alert_that_matches_no_run_resource_gives_an_empty_result_not_a_guess():
    service, _ = _service()
    result = service.calculate_blast_radius("mig-1", "aws:f2")
    assert result.compromised_node_id is None
    assert result.impacted_nodes == [] and result.max_risk_score is None
    assert result.reason


def test_no_safe_target_until_the_target_app_is_deployed():
    topology = {**TOPOLOGY, "nodes": [{**n, "state": "pending"} if n["id"] == "target_app" else n for n in TOPOLOGY["nodes"]]}
    service, _ = _service(topology=topology)
    assert service.calculate_blast_radius("mig-1", "aws:f1").safe_reroute_targets == []


def test_unknown_run_or_alert_is_404():
    service, _ = _service()
    with pytest.raises(HTTPException) as exc:
        service.calculate_blast_radius("mig-unknown", "aws:f1")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        service.calculate_blast_radius("mig-1", "aws:gone")
    assert exc.value.status_code == 404


def test_match_needs_the_same_cloud_and_a_meaningful_name():
    alert = RDS_ALERT.model_copy(update={"provider": "gcp"})
    assert match_node(alert, TOPOLOGY["nodes"]) is None
    short = [{"id": "x", "cloud": "aws", "resource": "db"}]
    assert match_node(RDS_ALERT, short) is None


class FakeWorker:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def set_quarantine(self, isolated_nodes, reroute_target):
        if self.fail:
            raise RuntimeError("down")
        self.calls.append((isolated_nodes, reroute_target))
        return {}


def test_reroute_and_release_go_through_the_worker(monkeypatch):
    from app.features.provisioning.worker_client import WorkerClient

    worker = FakeWorker()
    monkeypatch.setattr(WorkerClient, "from_env", lambda: worker)
    service, _ = _service()
    res = service.execute_reroute(RerouteRequest(compromised_node_id="source_db", target_reroute_id="target_app"))
    assert res.status == "rerouted" and res.cloudflare_synced
    assert worker.calls == [(["source_db"], "target_app")]
    released = service.release_quarantine("source_db")
    assert released.status == "released" and released.active_target_id is None
    assert worker.calls[-1] == ([], None)


def test_reroute_without_a_worker_is_refused_instead_of_pretending(monkeypatch):
    from app.features.provisioning.worker_client import WorkerClient

    monkeypatch.setattr(WorkerClient, "from_env", lambda: None)
    service, _ = _service()
    with pytest.raises(HTTPException) as exc:
        service.execute_reroute(RerouteRequest(compromised_node_id="source_db", target_reroute_id="target_app"))
    assert exc.value.status_code == 503
    with pytest.raises(HTTPException) as exc:
        service.release_quarantine()
    assert exc.value.status_code == 503


def test_worker_failure_is_502_and_keeps_the_previous_state(monkeypatch):
    from app.features.provisioning.worker_client import WorkerClient

    monkeypatch.setattr(WorkerClient, "from_env", lambda: FakeWorker(fail=True))
    service, _ = _service()
    with pytest.raises(HTTPException) as exc:
        service.execute_reroute(RerouteRequest(compromised_node_id="source_db", target_reroute_id="target_app"))
    assert exc.value.status_code == 502
    assert service._quarantined_nodes == set()


def test_reroute_requires_a_target():
    service, _ = _service()
    with pytest.raises(HTTPException) as exc:
        service.execute_reroute(RerouteRequest(compromised_node_id="source_db", target_reroute_id=" "))
    assert exc.value.status_code == 400


@pytest.fixture
def client():
    service, _ = _service()
    app.dependency_overrides[get_security_service] = lambda: service
    app.dependency_overrides[require_local_access] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.pop(get_security_service, None)
    app.dependency_overrides.pop(require_local_access, None)


def test_api_returns_the_feed_and_the_blast_radius(client):
    feed = client.get("/security/alerts", params={"run_id": "mig-1"}).json()
    assert {a["id"] for a in feed["alerts"]} == {"aws:f1", "aws:f2"}
    assert feed["sources"][0]["status"] == "ok"
    res = client.post("/security/blast-radius", json={"run_id": "mig-1", "alert_id": "aws:f1"})
    assert res.status_code == 200 and res.json()["compromised_node_id"] == "source_db"


def test_routes_are_local_only():
    original = app.dependency_overrides.pop(require_local_access, None)
    try:
        assert TestClient(app).get("/security/alerts").status_code == 403
    finally:
        if original is not None:
            app.dependency_overrides[require_local_access] = original
