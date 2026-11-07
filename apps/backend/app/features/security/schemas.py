from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field

SecurityProvider = Literal["aws", "gcp", "azure", "onprem"]
AlertSeverity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
ResourceType = Literal["vm", "container", "database", "iam_role", "network"]


class SecurityAlert(BaseModel):
    id: str = Field(..., description="Unique alert identifier")
    provider: SecurityProvider = Field(..., description="Cloud or on-premise provider")
    severity: AlertSeverity = Field(..., description="Threat severity level")
    resource_type: ResourceType = Field(..., description="Type of resource impacted")
    resource_id: str = Field(..., description="Cloud resource ARN, GCP URI, or onprem ID")
    threat_category: str = Field(..., description="GuardDuty / SCC / IDS threat name")
    description: str = Field(..., description="Human-readable explanation of the finding")
    detected_at: datetime = Field(..., description="Timestamp of threat detection")


class BlastRadiusNode(BaseModel):
    node_id: str
    name: str
    provider: SecurityProvider
    resource_type: ResourceType
    risk_score: float = Field(..., ge=0.0, le=1.0, description="Calculated propagation risk score")
    hop_distance: int = Field(..., ge=0)
    dependency_type: str = Field(..., description="Relationship type e.g. direct_ingress, db_link")
    is_compromised: bool = False
    quarantined: bool = False


class BlastRadiusRequest(BaseModel):
    compromised_node_id: str
    depth_hops: int = Field(default=2, ge=1, le=5)
    alpha_decay: float = Field(default=0.45, ge=0.1, le=1.0)


class BlastRadiusResponse(BaseModel):
    root_alert: SecurityAlert
    compromised_node_id: str
    max_risk_score: float
    impacted_nodes: list[BlastRadiusNode]
    recommended_action: str
    safe_reroute_targets: list[str]


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
    active_target_id: str
    reroute_latency_ms: float
    message: str
    cloudflare_synced: bool
