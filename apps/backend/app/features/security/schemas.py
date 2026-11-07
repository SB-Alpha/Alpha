"""security 모듈 계약. 경보는 클라우드 보안 서비스(GuardDuty·Security Command Center·Defender for Cloud)에서만 옵니다.

고정 경보·고정 노드 그래프는 없습니다. 확인하지 못한 클라우드는 "0건" 이 아니라 sources[].status 로 이유를 알립니다.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SecurityProvider = Literal["aws", "gcp", "azure"]
AlertSeverity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
ResourceType = Literal["vm", "container", "database", "iam_role", "network", "storage", "other"]
# ok: 확인함(0건일 수 있음) / no_credentials: /db-migration/clouds 에 키가 없음 / no_access: 키에 읽기 권한이 없음
# not_enabled: 그 서비스가 꺼져 있음 / no_scope: 어느 리전을 볼지 모름 / error: 그 밖의 실패
SourceStatusValue = Literal["ok", "no_credentials", "no_access", "not_enabled", "no_scope", "error"]


class SecurityAlert(BaseModel):
    id: str = Field(..., description="클라우드가 준 경보 id (provider 접두어)")
    provider: SecurityProvider
    severity: AlertSeverity
    resource_type: ResourceType
    resource_id: str = Field(..., description="클라우드가 준 자원 식별자 (ARN, 리소스 이름, Azure 리소스 id)")
    threat_category: str = Field(..., description="GuardDuty finding type / SCC category / Defender alertType")
    description: str
    detected_at: datetime | None = None
    region: str | None = None


class SourceStatus(BaseModel):
    provider: SecurityProvider
    service: str  # "GuardDuty" | "Security Command Center" | "Defender for Cloud"
    status: SourceStatusValue
    scope: str | None = None  # 확인한 리전·프로젝트·구독
    count: int | None = None  # status 가 ok 일 때만
    checked_at: datetime
    message: str | None = None  # 비밀값·응답 원문 없이 짧게


class SecurityFeed(BaseModel):
    alerts: list[SecurityAlert]
    sources: list[SourceStatus]


class BlastRadiusNode(BaseModel):
    node_id: str  # run 토폴로지 노드 id (workflows/topology.py)
    name: str
    provider: str | None = None
    resource_type: ResourceType
    resource: str | None = None
    risk_score: float = Field(..., ge=0.0, le=1.0, description="경보 심각도 가중치 × exp(-alpha × hop)")
    hop_distance: int = Field(..., ge=0)
    dependency_type: str = Field(..., description="토폴로지 간선 종류 (app_db, replication, traffic)")
    is_compromised: bool = False
    quarantined: bool = False


class BlastRadiusRequest(BaseModel):
    run_id: str
    alert_id: str
    depth_hops: int = Field(default=2, ge=1, le=5)
    alpha_decay: float = Field(default=0.45, ge=0.1, le=1.0)


class BlastRadiusResponse(BaseModel):
    root_alert: SecurityAlert
    # 경보 자원과 맞는 run 노드가 없으면 None 이고 영향 노드는 비어 있습니다 (짐작하지 않음)
    compromised_node_id: str | None
    max_risk_score: float | None
    impacted_nodes: list[BlastRadiusNode]
    recommended_action: str | None
    safe_reroute_targets: list[str]
    reason: str | None = None


class RerouteRequest(BaseModel):
    compromised_node_id: str
    target_reroute_id: str
    quarantine: bool = True
    run_id: str | None = None


class ReleaseQuarantineRequest(BaseModel):
    node_id: str | None = Field(default=None, description="특정 노드 해제, 생략 시 전체 해제")


class RerouteResponse(BaseModel):
    status: Literal["rerouted", "quarantined", "released", "failed"]
    quarantined_node_id: str
    active_target_id: str | None
    reroute_latency_ms: float
    message: str
    cloudflare_synced: bool
