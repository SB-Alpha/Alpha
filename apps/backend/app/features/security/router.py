from typing import List
from fastapi import APIRouter, Depends, Query
from app.core.local_access import require_local_access
from .schemas import (
    BlastRadiusRequest,
    BlastRadiusResponse,
    ReleaseQuarantineRequest,
    RerouteRequest,
    RerouteResponse,
    SecurityAlert,
)
from .service import SecurityGovernanceService

router = APIRouter(prefix="/security", tags=["security-governance"])

# 싱글톤 서비스 인스턴스
_service = SecurityGovernanceService()


def get_security_service() -> SecurityGovernanceService:
    return _service


@router.get("/alerts", response_model=List[SecurityAlert])
def list_security_alerts(
    service: SecurityGovernanceService = Depends(get_security_service),
) -> List[SecurityAlert]:
    """AWS GuardDuty, GCP Security Command Center 등 통합된 보안 알림 목록을 반환합니다."""
    return service.get_alerts()


@router.post("/blast-radius", response_model=BlastRadiusResponse)
def compute_blast_radius(
    req: BlastRadiusRequest,
    service: SecurityGovernanceService = Depends(get_security_service),
) -> BlastRadiusResponse:
    """특정 침해 노드 기준 네트워크·DB 종속성 전파도(Blast Radius)를 연산합니다."""
    return service.calculate_blast_radius(
        compromised_node_id=req.compromised_node_id,
        depth_hops=req.depth_hops,
        alpha_decay=req.alpha_decay,
    )


@router.post("/reroute", response_model=RerouteResponse, dependencies=[Depends(require_local_access)])
def reroute_compromised_node(
    req: RerouteRequest,
    service: SecurityGovernanceService = Depends(get_security_service),
) -> RerouteResponse:
    """침해된 노드를 즉시 격리(Quarantine)하고 Cloudflare 에지를 통해 안전한 대체 CSP로 우회합니다."""
    return service.execute_reroute(req)


@router.post("/release", response_model=RerouteResponse, dependencies=[Depends(require_local_access)])
def release_quarantined_node(
    req: ReleaseQuarantineRequest | None = None,
    service: SecurityGovernanceService = Depends(get_security_service),
) -> RerouteResponse:
    """격리되었던 노드의 격리를 해제하고 정상 원본 트래픽으로 롤백 복원합니다."""
    node_id = req.node_id if req else None
    return service.release_quarantine(node_id)
