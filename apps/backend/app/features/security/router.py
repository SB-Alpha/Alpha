from fastapi import APIRouter, Depends, Query

from app.core.local_access import require_local_access

from .schemas import (
    BlastRadiusRequest,
    BlastRadiusResponse,
    ReleaseQuarantineRequest,
    RerouteRequest,
    RerouteResponse,
    SecurityFeed,
)
from .service import SecurityGovernanceService

# 클라우드 키로 보안 경보를 읽고 Worker 격리를 바꾸므로 모든 경로를 이 PC 에서만 부를 수 있게 합니다
router = APIRouter(prefix="/security", tags=["security-governance"], dependencies=[Depends(require_local_access)])

_service = SecurityGovernanceService()


def get_security_service() -> SecurityGovernanceService:
    return _service


@router.get("/alerts", response_model=SecurityFeed)
def list_security_alerts(
    run_id: str | None = Query(default=None, description="이 run 의 AWS 리전에서 GuardDuty 를 봅니다"),
    service: SecurityGovernanceService = Depends(get_security_service),
) -> SecurityFeed:
    """GuardDuty·Security Command Center·Defender for Cloud 의 실제 경보와, 클라우드마다 확인한 결과(sources)."""
    return service.get_feed(run_id)


@router.post("/blast-radius", response_model=BlastRadiusResponse)
def compute_blast_radius(
    req: BlastRadiusRequest,
    service: SecurityGovernanceService = Depends(get_security_service),
) -> BlastRadiusResponse:
    """실제 경보 하나를 run 토폴로지의 노드에 맞추고, 간선을 따라 영향 범위를 계산합니다."""
    return service.calculate_blast_radius(req.run_id, req.alert_id, req.depth_hops, req.alpha_decay)


@router.post("/reroute", response_model=RerouteResponse)
def reroute_compromised_node(
    req: RerouteRequest,
    service: SecurityGovernanceService = Depends(get_security_service),
) -> RerouteResponse:
    """노드를 격리하고 Cloudflare Worker 로 대체 경로로 우회합니다. Worker 가 없으면 503."""
    return service.execute_reroute(req)


@router.post("/release", response_model=RerouteResponse)
def release_quarantined_node(
    req: ReleaseQuarantineRequest | None = None,
    service: SecurityGovernanceService = Depends(get_security_service),
) -> RerouteResponse:
    """격리를 풀고 Worker 에 반영합니다. Worker 가 없으면 503."""
    return service.release_quarantine(req.node_id if req else None)
