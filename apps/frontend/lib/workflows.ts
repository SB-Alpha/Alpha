// workflows API 클라이언트. 타입은 backend `app/features/workflows/schemas.py` 와 같은 모양입니다.
// 요청은 Next.js rewrite(`/api/*` → WORKFLOWS_API_URL)를 거쳐 백엔드로 갑니다.

import { adapterFor, type TargetInfraConfig } from "./adapters/types";

export type WorkflowState =
  | "pending"
  | "running"
  | "waiting_approval"
  | "paused"
  | "succeeded"
  | "failed"
  | "aborted";

export type StageName = "discovery" | "planning" | "provisioning" | "db_migration" | "app_deploy" | "cutover";

export type Gate = "plan" | "cutover";

export const STAGE_ORDER: StageName[] = ["discovery", "planning", "provisioning", "db_migration", "app_deploy", "cutover"];

export const GATE_BEFORE: Partial<Record<StageName, Gate>> = { provisioning: "plan", cutover: "cutover" };

/** 원본 클라우드. aws 는 기존 조회, gcp·azure 는 cloud_source 위치의 앱·DB 를 클라우드 API 로 조회합니다. */
export type SourceKind = "aws" | "gcp" | "azure";

/** 원본이 GCP·Azure 일 때 원본 위치. 비밀은 secret:// 이름표만 받습니다. */
export interface CloudSourceRequest {
  region: string;
  app_name: string;
  db_instance: string;
  credentials_ref?: string | null;
  project_id?: string | null; // GCP
  subscription_id?: string | null; // Azure
  resource_group?: string | null; // Azure
}

export interface MigrationRequest {
  github_repo_url?: string | null;
  service_name?: string | null;
  /** AWS 원본일 때만 보냅니다. GCP·Azure 원본은 cloud_source.region 을 씁니다. */
  aws_region?: string | null;
  aws_role_ref?: string | null;
  aws_credentials_ref?: string | null;
  ecs_service?: string | null;
  rds_instance_id?: string | null;
  db_credentials_ref?: string | null;
  db_admin_ref?: string | null;
  target_region?: string;
  target_infra?: TargetInfraConfig | null;
  source_kind?: SourceKind | "manual";
  cloud_source?: CloudSourceRequest | null;
}

/** 화면에 보이는 run 이름. 예전 요청에는 저장소 주소만 있습니다. */
export function runTitle(request: MigrationRequest): string {
  const repository = request.github_repo_url?.replace(/^https?:\/\/(www\.)?github\.com\//, "");
  return request.service_name || repository || request.ecs_service || "이름 없는 서비스";
}

const SOURCE_NAME: Record<string, string> = { aws: "AWS", gcp: "GCP", azure: "Azure", manual: "수동 입력" };

/** 원본 클라우드와 리전. AWS 는 aws_region, GCP·Azure 는 cloud_source.region 입니다. */
export function sourceSummary(request: MigrationRequest): string {
  const kind = request.source_kind ?? "aws";
  const region = kind === "aws" ? request.aws_region : request.cloud_source?.region;
  return `${SOURCE_NAME[kind] ?? kind} ${region || "미제공"}`;
}

export function targetSummary(request: MigrationRequest): string {
  return `${adapterFor(request.target_infra?.provider).cloudName} ${request.target_region ?? "—"}`;
}

export interface StageStatus {
  stage: StageName;
  state: WorkflowState;
  progress: number;
  metrics: Record<string, unknown>;
  message: string | null;
  manual: boolean;
  updated_at: string | null;
}

export interface RunStatus {
  run_id: string;
  state: WorkflowState;
  current_stage: StageName | null;
  waiting_gate: Gate | null;
  approvals: Gate[];
  request: MigrationRequest;
  stages: StageStatus[];
  outputs: Record<string, unknown>;
  message: string | null;
  created_at: string;
  updated_at: string;
  approval_records: ApprovalRecord[];
  cutover_readiness?: CutoverReadiness | null; // 승인 ② 대기 중일 때만 서버가 채웁니다
  cleanup_state?: "running" | "done" | "failed" | null; // 복제 자원 정리 상태 (중단 뒤, 또는 전환이 끝난 뒤 이전 자원 정리)
  cleanup_message?: string | null;
  cdc_job_id?: string | null; // 단건 응답에만 있습니다. CDC 작업 직접 제어에 씁니다
}

export type CheckStatus = "pass" | "fail" | "unknown";

export type CutoverDecision = "go" | "needs_confirmation" | "no_go";

export interface ReadinessCheck {
  id: string;
  title: string;
  status: CheckStatus;
  detail: string;
}

export interface CutoverReadiness {
  decision: CutoverDecision;
  checks: ReadinessCheck[];
}

export type PreflightStatus = "pass" | "fail" | "unknown" | "info";

export type PreflightVerdict = "go" | "no_go" | "unverified";

export interface PreflightCheck {
  id: string;
  label: string;
  status: PreflightStatus;
  detail?: string | null;
  value?: number | string | null;
}

export interface PreflightReport {
  run_id: string;
  gate: Gate;
  verdict: PreflightVerdict;
  advisory: boolean;
  checks: PreflightCheck[];
  measured_at: string;
}

export interface ApprovalRecord {
  gate: Gate;
  approver?: string | null;
  comment?: string | null;
  preflight_verdict?: PreflightVerdict | null;
  preflight_measured_at?: string | null;
  readiness_decision?: CutoverDecision | null;
  at: string;
}

/** 승인 버튼을 누를 때 화면에 표시된 점검 결과입니다. 없으면 null 입니다. */
export type DisplayedPreflight = { verdict: PreflightVerdict; measured_at: string } | null;

export interface SelfHealingDiagnosis {
  category: string;
  root_cause: string;
  action_plan: string[];
  suggested_manual_input?: Record<string, unknown> | null;
  diagnosed_by: "ai" | "rule" | "reasoning_bank";
  /** 과거에 풀린 같은 장애의 기억으로 낸 진단인지 (ReasoningBank, #77) */
  memory_hit?: boolean;
  /** 그 기억의 과거 성공률 (0~1). 기억이 아니면 없음 */
  prior_success_rate?: number | null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const BASE = "/api/runs";

const call = <T>(path: string, init?: RequestInit) => request<T>(`${BASE}${path}`, init);

export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      headers: { "content-type": "application/json", ...init?.headers },
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "백엔드에 연결할 수 없습니다. workflows 서버가 켜져 있는지 확인하세요.");
  }
  if (!res.ok) {
    throw new ApiError(res.status, await errorMessage(res));
  }
  return (await res.json()) as T;
}

async function errorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return "입력값을 확인하세요 (형식 오류).";
  } catch {
    // 본문이 JSON 이 아니면 상태 코드로만 알림
  }
  return `요청 실패 (HTTP ${res.status})`;
}

const post = <T>(path: string, body?: unknown) =>
  call<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const workflows = {
  list: () => call<RunStatus[]>(""),
  get: (runId: string) => call<RunStatus>(`/${encodeURIComponent(runId)}`),
  create: (req: MigrationRequest) => post<RunStatus>("", req),
  approve: (
    runId: string,
    gate: Gate,
    comment?: string,
    approver?: string,
    preflight?: DisplayedPreflight,
    acknowledgeUnknown = false,
  ) =>
    post<RunStatus>(`/${encodeURIComponent(runId)}/approve`, {
      gate,
      comment: comment || null,
      approver: approver?.trim() || null,
      preflight_verdict: preflight?.verdict ?? null,
      preflight_measured_at: preflight?.measured_at ?? null,
      acknowledge_unknown: acknowledgeUnknown,
    }),
  abort: (runId: string, reason?: string) =>
    post<RunStatus>(`/${encodeURIComponent(runId)}/abort`, { reason: reason || null }),
  resume: (runId: string) => post<RunStatus>(`/${encodeURIComponent(runId)}/resume`),
  retryCleanup: (runId: string) => post<RunStatus>(`/${encodeURIComponent(runId)}/cleanup`),
  // 전환이 끝난 run 의 복제 작업·VPN 정리. 실패하면 같은 요청으로 다시 시도합니다.
  decommission: (runId: string) => post<RunStatus>(`/${encodeURIComponent(runId)}/decommission`),
  submitManual: (runId: string, stage: StageName, output: Record<string, unknown>, note?: string) =>
    post<RunStatus>(`/${encodeURIComponent(runId)}/stages/${stage}/manual`, { output, note: note || null }),
  getDiagnosis: (runId: string) => call<SelfHealingDiagnosis>(`/${encodeURIComponent(runId)}/diagnosis`),
  triggerDiagnosis: (runId: string) => post<SelfHealingDiagnosis>(`/${encodeURIComponent(runId)}/diagnose`),
  getPreflight: (runId: string) => call<PreflightReport>(`/${encodeURIComponent(runId)}/preflight`),
};
