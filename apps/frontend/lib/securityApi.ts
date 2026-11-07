import { request } from "@/lib/workflows";

export interface SecurityAlert {
  id: string;
  provider: "aws" | "gcp" | "azure" | "onprem";
  severity: "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
  resource_type: "vm" | "container" | "database" | "iam_role" | "network";
  resource_id: string;
  threat_category: string;
  description: string;
  detected_at: string;
}

export interface BlastRadiusNode {
  node_id: string;
  name: string;
  provider: "aws" | "gcp" | "azure" | "onprem";
  resource_type: string;
  risk_score: number;
  hop_distance: number;
  dependency_type: string;
  is_compromised: boolean;
  quarantined: boolean;
}

export interface BlastRadiusResponse {
  root_alert: SecurityAlert;
  compromised_node_id: string;
  max_risk_score: number;
  impacted_nodes: BlastRadiusNode[];
  recommended_action: string;
  safe_reroute_targets: string[];
}

export interface RerouteResponse {
  status: "rerouted" | "quarantined" | "released" | "failed";
  quarantined_node_id: string;
  active_target_id: string;
  reroute_latency_ms: number;
  message: string;
  cloudflare_synced: boolean;
}

export const securityApi = {
  getAlerts: () => request<SecurityAlert[]>("/api/security/alerts"),

  computeBlastRadius: (compromisedNodeId: string = "aws-ecs-workitems") =>
    request<BlastRadiusResponse>("/api/security/blast-radius", {
      method: "POST",
      body: JSON.stringify({ compromised_node_id: compromisedNodeId }),
    }),

  executeReroute: (compromisedNodeId: string = "aws-ecs-workitems", targetRerouteId: string = "gcp-cloud-run") =>
    request<RerouteResponse>("/api/security/reroute", {
      method: "POST",
      body: JSON.stringify({
        compromised_node_id: compromisedNodeId,
        target_reroute_id: targetRerouteId,
        quarantine: true,
      }),
    }),

  releaseQuarantine: (nodeId?: string) =>
    request<RerouteResponse>("/api/security/release", {
      method: "POST",
      body: JSON.stringify({ node_id: nodeId ?? null }),
    }),
};
