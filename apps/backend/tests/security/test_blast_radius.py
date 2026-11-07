import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.features.security.service import SecurityGovernanceService
from app.features.security.schemas import RerouteRequest


def test_list_security_alerts():
    client = TestClient(app)
    resp = client.get("/security/alerts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2
    aws_alert = next((a for a in data if a["provider"] == "aws"), None)
    assert aws_alert is not None
    assert aws_alert["severity"] == "CRITICAL"
    assert "GuardDuty" in aws_alert["description"]


def test_calculate_blast_radius_decay():
    service = SecurityGovernanceService()
    result = service.calculate_blast_radius(
        compromised_node_id="aws-ecs-workitems",
        depth_hops=2,
        alpha_decay=0.45,
    )
    assert result.compromised_node_id == "aws-ecs-workitems"
    assert result.max_risk_score == 1.0  # CRITICAL weight

    # 루트 노드 검증
    root_node = next(n for n in result.impacted_nodes if n.node_id == "aws-ecs-workitems")
    assert root_node.hop_distance == 0
    assert root_node.risk_score == 1.0
    assert root_node.is_compromised is True

    # 1-hop DB 노드 검증 (감쇄: 1.0 * exp(-0.45 * 1) ≈ 0.638)
    db_node = next(n for n in result.impacted_nodes if n.node_id == "aws-rds-postgres")
    assert db_node.hop_distance == 1
    assert 0.60 <= db_node.risk_score <= 0.65
    assert db_node.dependency_type == "database_link"

    # 2-hop EBS 스토리지 검증 (감쇄: 1.0 * exp(-0.45 * 2) ≈ 0.407)
    ebs_node = next(n for n in result.impacted_nodes if n.node_id == "aws-ebs-volume")
    assert ebs_node.hop_distance == 2
    assert 0.38 <= ebs_node.risk_score <= 0.43


def test_blast_radius_api_endpoint():
    client = TestClient(app)
    resp = client.post(
        "/security/blast-radius",
        json={
            "compromised_node_id": "aws-ecs-workitems",
            "depth_hops": 2,
            "alpha_decay": 0.45,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["compromised_node_id"] == "aws-ecs-workitems"
    assert len(body["impacted_nodes"]) >= 3
    assert "gcp-cloud-run" in body["safe_reroute_targets"]


def test_reroute_and_quarantine_with_worker_sync(monkeypatch):
    from app.features.provisioning.worker_client import WorkerClient

    calls = []

    class FakeWorker:
        def set_quarantine(self, isolated_nodes, reroute_target):
            calls.append((isolated_nodes, reroute_target))
            return {"isolated_nodes": isolated_nodes, "reroute_target": reroute_target}

    monkeypatch.setattr(WorkerClient, "from_env", lambda: FakeWorker())
    client = TestClient(app)
    resp = client.post(
        "/security/reroute",
        json={
            "compromised_node_id": "aws-ecs-workitems",
            "target_reroute_id": "gcp-cloud-run",
            "quarantine": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rerouted"
    assert body["quarantined_node_id"] == "aws-ecs-workitems"
    assert body["active_target_id"] == "gcp-cloud-run"
    assert body["reroute_latency_ms"] < 500  # < 500ms SLA
    assert body["cloudflare_synced"] is True
    assert len(calls) == 1
    assert "aws-ecs-workitems" in calls[0][0]


def test_reroute_simulation_when_worker_not_configured(monkeypatch):
    from app.features.provisioning.worker_client import WorkerClient

    monkeypatch.setattr(WorkerClient, "from_env", lambda: None)
    client = TestClient(app)
    resp = client.post(
        "/security/reroute",
        json={
            "compromised_node_id": "aws-ecs-workitems",
            "target_reroute_id": "gcp-cloud-run",
            "quarantine": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cloudflare_synced"] is False
    assert body["status"] == "quarantined"
    assert "시뮬레이션" in body["message"]
    # Juho 리뷰 반영: Worker 미설정 시뮬레이션에서는 "우회되었습니다" 문구를 노출하지 않음
    assert "우회되었습니다" not in body["message"]


def test_reroute_requires_target():
    client = TestClient(app)
    resp = client.post(
        "/security/reroute",
        json={
            "compromised_node_id": "aws-ecs-workitems",
            "target_reroute_id": "",
            "quarantine": True,
        },
    )
    assert resp.status_code == 400
    assert "reroute_target_required" in resp.json()["detail"]


def test_reroute_worker_sync_failure_returns_502_and_rolls_back(monkeypatch):
    from app.features.provisioning.worker_client import WorkerClient
    from app.features.provisioning.terraform import ProvisioningError
    from app.features.security.router import get_security_service

    class FailingWorker:
        def set_quarantine(self, isolated_nodes, reroute_target):
            raise ProvisioningError("Worker endpoint connection timeout")

    monkeypatch.setattr(WorkerClient, "from_env", lambda: FailingWorker())
    client = TestClient(app)
    resp = client.post(
        "/security/reroute",
        json={
            "compromised_node_id": "node-fail-test",
            "target_reroute_id": "gcp-cloud-run",
            "quarantine": True,
        },
    )
    assert resp.status_code == 502
    assert "Worker quarantine sync failed" in resp.json()["detail"]

    # 롤백 확인: 실패한 노드가 로컬 격리 목록에 추가되지 않음
    service = get_security_service()
    assert "node-fail-test" not in service._quarantined_nodes


def test_release_quarantine(monkeypatch):
    from app.features.provisioning.worker_client import WorkerClient

    calls = []

    class FakeWorker:
        def set_quarantine(self, isolated_nodes, reroute_target):
            calls.append((isolated_nodes, reroute_target))
            return {"isolated_nodes": isolated_nodes, "reroute_target": reroute_target}

    monkeypatch.setattr(WorkerClient, "from_env", lambda: FakeWorker())
    client = TestClient(app)
    resp = client.post("/security/release", json={"node_id": "aws-ecs-workitems"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "released"
    assert body["cloudflare_synced"] is True
    assert len(calls) == 1
    assert calls[0][0] == []


def test_release_quarantine_worker_failure_returns_502(monkeypatch):
    from app.features.provisioning.worker_client import WorkerClient
    from app.features.provisioning.terraform import ProvisioningError

    class FailingWorker:
        def set_quarantine(self, isolated_nodes, reroute_target):
            raise ProvisioningError("Worker release failed")

    monkeypatch.setattr(WorkerClient, "from_env", lambda: FailingWorker())
    client = TestClient(app)
    resp = client.post("/security/release", json={"node_id": "aws-ecs-workitems"})
    assert resp.status_code == 502
    assert "Worker quarantine release failed" in resp.json()["detail"]
