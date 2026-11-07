// 보안 API (backend app/features/security). 경보는 GuardDuty·Security Command Center·Defender for Cloud 에서만 옵니다.
// 확인하지 못한 클라우드는 sources[].status 로 이유가 오며, "0건" 과 구분해 보여 줍니다.

import { request } from "@/lib/workflows";

export type SecurityProvider = "aws" | "gcp" | "azure";
export type SourceStatusValue = "ok" | "no_credentials" | "no_access" | "not_enabled" | "no_scope" | "error";

export interface SecurityAlert {
  id: string;
  provider: SecurityProvider;
  severity: "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
  resource_type: string;
  resource_id: string;
  threat_category: string;
  description: string;
  detected_at: string | null;
  region: string | null;
}

export interface SourceStatus {
  provider: SecurityProvider;
  service: string;
  status: SourceStatusValue;
  scope: string | null;
  count: number | null;
  checked_at: string;
  message: string | null;
}

export interface SecurityFeed {
  alerts: SecurityAlert[];
  sources: SourceStatus[];
}

export interface BlastRadiusNode {
  node_id: string;
  name: string;
  provider: string | null;
  resource_type: string;
  resource: string | null;
  risk_score: number;
  hop_distance: number;
  dependency_type: string;
  is_compromised: boolean;
  quarantined: boolean;
}

export interface BlastRadiusResponse {
  root_alert: SecurityAlert;
  compromised_node_id: string | null;
  max_risk_score: number | null;
  impacted_nodes: BlastRadiusNode[];
  recommended_action: string | null;
  safe_reroute_targets: string[];
  reason: string | null;
}

export interface RerouteResponse {
  status: "rerouted" | "quarantined" | "released" | "failed";
  quarantined_node_id: string;
  active_target_id: string | null;
  reroute_latency_ms: number;
  message: string;
  cloudflare_synced: boolean;
}

export const SOURCE_STATUS_LABEL: Record<SourceStatusValue, string> = {
  ok: "확인함",
  no_credentials: "키 없음",
  no_access: "권한 없음",
  not_enabled: "서비스 꺼짐",
  no_scope: "리전 모름",
  error: "확인 실패",
};

const post = <T>(url: string, body: unknown) => request<T>(url, { method: "POST", body: JSON.stringify(body) });

export const securityApi = {
  getFeed: (runId: string) => request<SecurityFeed>(`/api/security/alerts?run_id=${encodeURIComponent(runId)}`),
  computeBlastRadius: (runId: string, alertId: string) =>
    post<BlastRadiusResponse>("/api/security/blast-radius", { run_id: runId, alert_id: alertId }),
  executeReroute: (runId: string, compromisedNodeId: string, targetRerouteId: string) =>
    post<RerouteResponse>("/api/security/reroute", {
      compromised_node_id: compromisedNodeId,
      target_reroute_id: targetRerouteId,
      quarantine: true,
      run_id: runId,
    }),
  releaseQuarantine: (nodeId: string | null) => post<RerouteResponse>("/api/security/release", { node_id: nodeId }),
};
