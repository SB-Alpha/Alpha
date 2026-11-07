import math
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from .schemas import (
    AlertSeverity,
    BlastRadiusNode,
    BlastRadiusResponse,
    RerouteRequest,
    RerouteResponse,
    SecurityAlert,
)

SEVERITY_WEIGHTS: Dict[AlertSeverity, float] = {
    "CRITICAL": 1.0,
    "HIGH": 0.8,
    "MEDIUM": 0.5,
    "LOW": 0.2,
}

# 기본 토폴로지 의존성 그래프 (노드 간 연결 관계)
DEFAULT_TOPOLOGY_EDGES = {
    "aws-ecs-workitems": [
        ("aws-rds-postgres", "database_link"),
        ("aws-iam-task-execution", "iam_trust"),
        ("onprem-vpn-gateway", "vpn_peering"),
    ],
    "aws-rds-postgres": [
        ("aws-ebs-volume", "storage_mount"),
        ("aws-kms-key", "crypto_key"),
    ],
    "onprem-vpn-gateway": [
        ("onprem-cdc-node", "network_tunnel"),
    ],
    "gcp-cloud-run": [
        ("gcp-cloud-sql", "database_link"),
    ],
}

DEFAULT_NODE_METADATA = {
    "aws-ecs-workitems": {
        "name": "AWS ECS Fargate (work-items-app)",
        "provider": "aws",
        "resource_type": "container",
    },
    "aws-rds-postgres": {
        "name": "AWS RDS PostgreSQL (db-alpha-prod)",
        "provider": "aws",
        "resource_type": "database",
    },
    "aws-iam-task-execution": {
        "name": "AWS IAM Role (ecsTaskExecutionRole)",
        "provider": "aws",
        "resource_type": "iam_role",
    },
    "aws-ebs-volume": {
        "name": "AWS EBS gp3 Volume (vol-03f918e7)",
        "provider": "aws",
        "resource_type": "vm",
    },
    "aws-kms-key": {
        "name": "AWS KMS CMK (alias/alpha-rds-key)",
        "provider": "aws",
        "resource_type": "iam_role",
    },
    "onprem-vpn-gateway": {
        "name": "On-Premises IPsec DRG Tunnel",
        "provider": "onprem",
        "resource_type": "network",
    },
    "onprem-cdc-node": {
        "name": "On-Prem Debezium CDC Connector",
        "provider": "onprem",
        "resource_type": "container",
    },
    "gcp-cloud-run": {
        "name": "GCP Cloud Run (target-app-service)",
        "provider": "gcp",
        "resource_type": "container",
    },
    "gcp-cloud-sql": {
        "name": "GCP Cloud SQL PostgreSQL (alpha-replica)",
        "provider": "gcp",
        "resource_type": "database",
    },
}


class SecurityGovernanceService:
    def __init__(self) -> None:
        self._alerts: List[SecurityAlert] = [
            SecurityAlert(
                id="sec-alert-gd-001",
                provider="aws",
                severity="CRITICAL",
                resource_type="container",
                resource_id="arn:aws:ecs:ap-northeast-2:123456789012:task/work-items-app/aws-ecs-workitems",
                threat_category="UnauthorizedAccess:EC2/MaliciousIPCaller.Custom",
                description="GuardDuty detected outbound communication to a known Tor exit node with abnormal egress traffic spikes (CVE-2026-4192 RCE suspect, 데모 시뮬레이션 데이터).",
                detected_at=datetime.now(timezone.utc),
            ),
            SecurityAlert(
                id="sec-alert-scc-002",
                provider="gcp",
                severity="LOW",
                resource_type="database",
                resource_id="//cloudresourcemanager.googleapis.com/projects/softbank-alpha/cloudsql/instances/alpha-replica",
                threat_category="PUBLIC_SQL_INSTANCE_DISABLED",
                description="SCC audit verified Cloud SQL instance enforces Private Services Access (VPC peering only, 0 public IP, 데모 시뮬레이션 데이터).",
                detected_at=datetime.now(timezone.utc),
            ),
        ]
        self._quarantined_nodes: set[str] = set()

    def get_alerts(self) -> List[SecurityAlert]:
        return self._alerts

    def calculate_blast_radius(
        self,
        compromised_node_id: str = "aws-ecs-workitems",
        depth_hops: int = 2,
        alpha_decay: float = 0.45,
    ) -> BlastRadiusResponse:
        # 루트 알림 매칭: node_id가 resource_id에 포함된 알림 검색
        matched_alert = next(
            (a for a in self._alerts if compromised_node_id and compromised_node_id in a.resource_id),
            None,
        )

        if matched_alert:
            root_alert = matched_alert
            base_weight = SEVERITY_WEIGHTS.get(root_alert.severity, 0.5)
        else:
            # 알림이 없는 임의 노드일 경우 기본 시뮬레이션 알림 생성
            root_alert = SecurityAlert(
                id=f"sec-sim-{compromised_node_id}",
                provider=DEFAULT_NODE_METADATA.get(compromised_node_id, {}).get("provider", "aws"),
                severity="LOW",
                resource_type=DEFAULT_NODE_METADATA.get(compromised_node_id, {}).get("resource_type", "vm"),
                resource_id=f"simulated://{compromised_node_id}",
                threat_category="SuspiciousActivity:GenericSimulation",
                description=f"시뮬레이션: [{compromised_node_id}] 잠재적 위협 탐지 (데모 데이터)",
                detected_at=datetime.now(timezone.utc),
            )
            base_weight = SEVERITY_WEIGHTS["LOW"]

        # BFS 로 의존성 그래프 탐색 및 전파 점수 연산: S(v) = weight * exp(-alpha * hop)
        visited = {compromised_node_id: 0}
        queue = [(compromised_node_id, 0)]
        nodes: List[BlastRadiusNode] = []

        # 0 hop (루트 침해 노드)
        meta_root = DEFAULT_NODE_METADATA.get(
            compromised_node_id,
            {"name": compromised_node_id, "provider": root_alert.provider, "resource_type": root_alert.resource_type},
        )
        nodes.append(
            BlastRadiusNode(
                node_id=compromised_node_id,
                name=meta_root["name"],
                provider=meta_root["provider"],
                resource_type=meta_root["resource_type"],
                risk_score=round(base_weight, 3),
                hop_distance=0,
                dependency_type="direct_ingress",
                is_compromised=True,
                quarantined=compromised_node_id in self._quarantined_nodes,
            )
        )

        while queue:
            curr_node, curr_hop = queue.pop(0)
            if curr_hop >= depth_hops:
                continue

            for neighbor, dep_type in DEFAULT_TOPOLOGY_EDGES.get(curr_node, []):
                next_hop = curr_hop + 1
                if neighbor not in visited or visited[neighbor] > next_hop:
                    visited[neighbor] = next_hop
                    queue.append((neighbor, next_hop))

                    # 감쇄 공식: S = base_weight * exp(-alpha * hop)
                    risk_score = round(base_weight * math.exp(-alpha_decay * next_hop), 3)
                    meta = DEFAULT_NODE_METADATA.get(
                        neighbor,
                        {"name": neighbor, "provider": "aws", "resource_type": "vm"},
                    )
                    nodes.append(
                        BlastRadiusNode(
                            node_id=neighbor,
                            name=meta["name"],
                            provider=meta["provider"],
                            resource_type=meta["resource_type"],
                            risk_score=risk_score,
                            hop_distance=next_hop,
                            dependency_type=dep_type,
                            is_compromised=False,
                            quarantined=neighbor in self._quarantined_nodes,
                        )
                    )

        safe_targets = ["gcp-cloud-run", "azure-container-app"]

        return BlastRadiusResponse(
            root_alert=root_alert,
            compromised_node_id=compromised_node_id,
            max_risk_score=base_weight,
            impacted_nodes=nodes,
            recommended_action=(
                f"침해된 [{compromised_node_id}] 인그레스 차단 및 무중단으로 "
                f"GCP Cloud Run [{safe_targets[0]}] 으로 트래픽 우회 권장"
            ),
            safe_reroute_targets=safe_targets,
        )

    def execute_reroute(self, request: RerouteRequest) -> RerouteResponse:
        start_ts = time.perf_counter()

        from fastapi import HTTPException
        from app.features.provisioning.worker_client import WorkerClient

        # 1. 대상 타깃 검증 (Juho 리뷰 반영: 격리 전 target 설정 여부 확인)
        if not request.target_reroute_id or not request.target_reroute_id.strip():
            raise HTTPException(status_code=400, detail="reroute_target_required: target_reroute_id must be provided")

        worker = None
        try:
            worker = WorkerClient.from_env()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Worker configuration error: {exc}") from None

        if worker is not None:
            # Worker 가 설정되어 있는 경우: Worker 동기화를 먼저 호출하고 실패 시 502 및 상태 롤백
            candidate_isolated = set(self._quarantined_nodes)
            if request.quarantine:
                candidate_isolated.add(request.compromised_node_id)
            else:
                candidate_isolated.discard(request.compromised_node_id)

            try:
                worker.set_quarantine(
                    isolated_nodes=list(candidate_isolated),
                    reroute_target=request.target_reroute_id,
                )
            except Exception as exc:
                # Worker 호출 실패 시: 로컬 격리 상태를 변경하지 않고 502 Bad Gateway 반환
                raise HTTPException(
                    status_code=502,
                    detail=f"Worker quarantine sync failed: {exc}",
                ) from None

            # Worker 동기화 성공 시에만 로컬 격리 목록 반영
            if request.quarantine:
                self._quarantined_nodes.add(request.compromised_node_id)
            else:
                self._quarantined_nodes.discard(request.compromised_node_id)

            elapsed_ms = round((time.perf_counter() - start_ts) * 1000, 2)
            return RerouteResponse(
                status="rerouted",
                quarantined_node_id=request.compromised_node_id,
                active_target_id=request.target_reroute_id,
                reroute_latency_ms=elapsed_ms,
                message=(
                    f"보안 위험 노드 [{request.compromised_node_id}] 가 격리되었습니다. "
                    f"트래픽이 [{request.target_reroute_id}] 으로 우회되었습니다 "
                    f"(Cloudflare Worker 에지 동기화 완료, 처리 시간: {elapsed_ms}ms)."
                ),
                cloudflare_synced=True,
            )

        # Worker 미설정 (시뮬레이션/데모 모드):
        # cloudflare_synced=False 로 두고 메시지에서 "우회되었습니다"는 제외
        if request.quarantine:
            self._quarantined_nodes.add(request.compromised_node_id)
        else:
            self._quarantined_nodes.discard(request.compromised_node_id)

        elapsed_ms = round((time.perf_counter() - start_ts) * 1000, 2)
        return RerouteResponse(
            status="quarantined",
            quarantined_node_id=request.compromised_node_id,
            active_target_id=request.target_reroute_id,
            reroute_latency_ms=elapsed_ms,
            message=(
                f"보안 위험 노드 [{request.compromised_node_id}] 격리 시뮬레이션 완료 "
                f"(Worker 미설정, 데모 모드, 처리 시간: {elapsed_ms}ms)."
            ),
            cloudflare_synced=False,
        )

    def release_quarantine(self, node_id: str | None = None) -> RerouteResponse:
        start_ts = time.perf_counter()

        from fastapi import HTTPException
        from app.features.provisioning.worker_client import WorkerClient

        target_name = node_id or "전체 노드"

        worker = None
        try:
            worker = WorkerClient.from_env()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Worker configuration error: {exc}") from None

        if worker is not None:
            candidate_isolated = set(self._quarantined_nodes)
            if node_id:
                candidate_isolated.discard(node_id)
            else:
                candidate_isolated.clear()

            try:
                worker.set_quarantine(
                    isolated_nodes=list(candidate_isolated),
                    reroute_target=None if not candidate_isolated else "gcp-cloud-run",
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Worker quarantine release failed: {exc}",
                ) from None

            if node_id:
                self._quarantined_nodes.discard(node_id)
            else:
                self._quarantined_nodes.clear()

            elapsed_ms = round((time.perf_counter() - start_ts) * 1000, 2)
            return RerouteResponse(
                status="released",
                quarantined_node_id=target_name,
                active_target_id="aws-ecs-workitems",
                reroute_latency_ms=elapsed_ms,
                message=f"[{target_name}] 격리 해제 완료 (Cloudflare Worker 에지 동기화 완료, 처리 시간: {elapsed_ms}ms).",
                cloudflare_synced=True,
            )

        # Worker 미설정 (시뮬레이션 모드)
        if node_id:
            self._quarantined_nodes.discard(node_id)
        else:
            self._quarantined_nodes.clear()

        elapsed_ms = round((time.perf_counter() - start_ts) * 1000, 2)
        return RerouteResponse(
            status="released",
            quarantined_node_id=target_name,
            active_target_id="aws-ecs-workitems",
            reroute_latency_ms=elapsed_ms,
            message=f"[{target_name}] 격리 해제 시뮬레이션 완료 (Worker 미설정, 처리 시간: {elapsed_ms}ms).",
            cloudflare_synced=False,
        )
