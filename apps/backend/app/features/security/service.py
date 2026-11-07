"""보안 경보와 영향 범위(blast radius).

경보는 sources.py 가 클라우드 보안 서비스에서 읽은 것만 씁니다. 영향 범위는 run 의 실제 토폴로지
(workflows/topology.py 가 run 출력에서 만든 노드·간선) 위에서 계산합니다. security 가 workflows 를 직접
import 하지 않도록 토폴로지를 돌려주는 함수를 main.py 가 넣어 줍니다 (set_topology_provider).
경보 자원과 맞는 run 노드가 없으면 영향 범위를 지어내지 않고 빈 결과와 이유를 돌려줍니다.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from . import sources
from .schemas import (
    AlertSeverity,
    BlastRadiusNode,
    BlastRadiusResponse,
    ResourceType,
    RerouteRequest,
    RerouteResponse,
    SecurityAlert,
    SecurityFeed,
)

SEVERITY_WEIGHTS: dict[AlertSeverity, float] = {"CRITICAL": 1.0, "HIGH": 0.8, "MEDIUM": 0.5, "LOW": 0.2}

_KIND_TO_RESOURCE: dict[str, ResourceType] = {
    "app": "container",
    "database": "database",
    "cdc_agent": "container",
    "vpn": "network",
    "edge": "network",
}
# 이름이 너무 짧으면 다른 자원 id 안에 우연히 들어 있을 수 있어 맞추지 않습니다
MIN_MATCH_LENGTH = 4

TopologyProvider = Callable[[str], dict[str, Any] | None]


def match_node(alert: SecurityAlert, nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """경보 자원 id 안에 run 노드의 실제 자원 이름이 들어 있으면 그 노드입니다. 같은 클라우드 노드만 봅니다."""
    resource_id = alert.resource_id.lower()
    best = None
    for node in nodes:
        name = node.get("resource")
        if not isinstance(name, str) or len(name) < MIN_MATCH_LENGTH:
            continue
        if node.get("cloud") not in (None, alert.provider):
            continue
        if name.lower() in resource_id and (best is None or len(name) > len(best["resource"])):
            best = node
    return best


class SecurityGovernanceService:
    def __init__(self, fetch: Callable[[list[str]], tuple[list[SecurityAlert], list[Any]]] = sources.fetch_all) -> None:
        self._fetch = fetch
        self._topology: TopologyProvider | None = None
        self._quarantined_nodes: set[str] = set()
        self._reroute_target: str | None = None

    def set_topology_provider(self, provider: TopologyProvider) -> None:
        self._topology = provider

    def _run_topology(self, run_id: str | None) -> dict[str, Any] | None:
        if run_id is None or self._topology is None:
            return None
        return self._topology(run_id)

    # -- 경보 -------------------------------------------------------------------------------

    def get_feed(self, run_id: str | None = None) -> SecurityFeed:
        topology = self._run_topology(run_id)
        regions = sorted({
            n["region"] for n in (topology or {}).get("nodes", [])
            if n.get("cloud") == "aws" and isinstance(n.get("region"), str) and n["region"]
        })
        alerts, statuses = self._fetch(regions)
        return SecurityFeed(alerts=alerts, sources=statuses)

    # -- 영향 범위 ---------------------------------------------------------------------------

    def calculate_blast_radius(self, run_id: str, alert_id: str, depth_hops: int = 2,
                               alpha_decay: float = 0.45) -> BlastRadiusResponse:
        topology = self._run_topology(run_id)
        if topology is None:
            raise HTTPException(404, f"run {run_id} 의 토폴로지를 찾지 못했습니다")
        feed = self.get_feed(run_id)
        alert = next((a for a in feed.alerts if a.id == alert_id), None)
        if alert is None:
            raise HTTPException(404, "그 경보가 지금 클라우드 경보 목록에 없습니다 (해결됐거나 보관됐을 수 있음)")

        nodes = topology.get("nodes", [])
        root = match_node(alert, nodes)
        if root is None:
            return BlastRadiusResponse(
                root_alert=alert, compromised_node_id=None, max_risk_score=None, impacted_nodes=[],
                recommended_action=None, safe_reroute_targets=[],
                reason="경보 자원과 맞는 이 run 의 자원이 없어 영향 범위를 계산하지 않았습니다",
            )

        by_id = {n["id"]: n for n in nodes}
        adjacency: dict[str, list[tuple[str, str]]] = {}
        for e in topology.get("edges", []):
            a, b, kind = e.get("from"), e.get("to"), e.get("kind", "link")
            if a in by_id and b in by_id:
                adjacency.setdefault(a, []).append((b, kind))
                adjacency.setdefault(b, []).append((a, kind))

        weight = SEVERITY_WEIGHTS[alert.severity]
        impacted = [self._node(root, weight, 0, "compromised", True)]
        seen = {root["id"]: 0}
        queue = [root["id"]]
        while queue:
            current = queue.pop(0)
            hop = seen[current]
            if hop >= depth_hops:
                continue
            for neighbor, kind in adjacency.get(current, []):
                if neighbor in seen:
                    continue
                seen[neighbor] = hop + 1
                queue.append(neighbor)
                score = round(weight * math.exp(-alpha_decay * (hop + 1)), 3)
                impacted.append(self._node(by_id[neighbor], score, hop + 1, kind, False))

        safe = self._safe_targets(root, by_id)
        action = (f"[{root['label']}] 를 격리하고 트래픽을 [{by_id[safe[0]]['label']}] 로 돌리는 것을 검토하세요"
                  if safe else f"[{root['label']}] 를 격리할 수 있는 정상 대체 경로가 이 run 에 없습니다")
        return BlastRadiusResponse(
            root_alert=alert, compromised_node_id=root["id"], max_risk_score=weight, impacted_nodes=impacted,
            recommended_action=action, safe_reroute_targets=safe,
        )

    def _node(self, node: dict[str, Any], score: float, hop: int, dependency: str, compromised: bool) -> BlastRadiusNode:
        return BlastRadiusNode(
            node_id=node["id"], name=node.get("label") or node["id"], provider=node.get("cloud"),
            resource_type=_KIND_TO_RESOURCE.get(node.get("kind", ""), "other"), resource=node.get("resource"),
            risk_score=score, hop_distance=hop, dependency_type=dependency, is_compromised=compromised,
            quarantined=node["id"] in self._quarantined_nodes,
        )

    @staticmethod
    def _safe_targets(root: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[str]:
        """원본 쪽이 침해됐고 목적지 앱이 배포까지 끝났을 때만 목적지 앱을 대체 경로로 냅니다."""
        target = by_id.get("target_app")
        if root["id"].startswith("source_") and target and target.get("state") == "succeeded":
            return ["target_app"]
        return []

    # -- 격리·우회 (Cloudflare Worker) --------------------------------------------------------

    @staticmethod
    def _worker() -> Any:
        from app.features.provisioning.worker_client import WorkerClient

        try:
            worker = WorkerClient.from_env()
        except Exception as exc:  # noqa: BLE001 - 설정 오류는 종류만 알립니다
            raise HTTPException(502, f"Worker 설정 오류 ({type(exc).__name__})") from None
        if worker is None:
            # 예전에는 Worker 없이 '격리됨' 을 돌려줬습니다. 실제로 막지 않았으므로 거절합니다.
            raise HTTPException(503, "WORKER_URL 이 설정되지 않아 격리·우회를 하지 않았습니다")
        return worker

    def execute_reroute(self, request: RerouteRequest) -> RerouteResponse:
        if not request.target_reroute_id.strip():
            raise HTTPException(400, "target_reroute_id 가 필요합니다")
        start = time.perf_counter()
        worker = self._worker()
        isolated = set(self._quarantined_nodes)
        if request.quarantine:
            isolated.add(request.compromised_node_id)
        else:
            isolated.discard(request.compromised_node_id)
        try:
            worker.set_quarantine(isolated_nodes=sorted(isolated), reroute_target=request.target_reroute_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Worker 격리 동기화 실패 ({type(exc).__name__})") from None
        self._quarantined_nodes = isolated
        self._reroute_target = request.target_reroute_id
        elapsed = round((time.perf_counter() - start) * 1000, 2)
        return RerouteResponse(
            status="rerouted", quarantined_node_id=request.compromised_node_id,
            active_target_id=request.target_reroute_id, reroute_latency_ms=elapsed, cloudflare_synced=True,
            message=(f"[{request.compromised_node_id}] 를 격리하고 [{request.target_reroute_id}] 로 우회하도록 "
                     f"Worker 에 반영했습니다 ({elapsed}ms)."),
        )

    def release_quarantine(self, node_id: str | None = None) -> RerouteResponse:
        start = time.perf_counter()
        worker = self._worker()
        isolated = set(self._quarantined_nodes)
        if node_id:
            isolated.discard(node_id)
        else:
            isolated.clear()
        target = self._reroute_target if isolated else None
        try:
            worker.set_quarantine(isolated_nodes=sorted(isolated), reroute_target=target)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Worker 격리 해제 실패 ({type(exc).__name__})") from None
        self._quarantined_nodes = isolated
        self._reroute_target = target
        elapsed = round((time.perf_counter() - start) * 1000, 2)
        name = node_id or "전체 노드"
        return RerouteResponse(
            status="released", quarantined_node_id=name, active_target_id=target, reroute_latency_ms=elapsed,
            cloudflare_synced=True, message=f"[{name}] 격리 해제를 Worker 에 반영했습니다 ({elapsed}ms).",
        )
