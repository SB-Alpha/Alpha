// db_migration 의 /db-migration/* 계약 (apps/backend/app/features/db_migration/UI_API.md).
// 화면은 이 파일의 함수만 부르고, 값이 없으면 "미제공" 으로 둡니다. 0 이나 기본값으로 채우지 않습니다.

export type Cloud = "aws" | "gcp" | "azure";
export const CLOUDS: readonly Cloud[] = ["aws", "gcp", "azure"];
export const CLOUD_LABEL: Record<Cloud, string> = { aws: "AWS", gcp: "GCP", azure: "Azure" };

/** run_input.py REGION_PATTERNS 와 같은 식. 화면에서 먼저 막고, 최종 판정은 백엔드가 합니다. */
export const REGION_PATTERNS: Record<Cloud, RegExp> = {
  aws: /^[a-z]{2}(-[a-z]+)+-\d+$/,
  gcp: /^[a-z]+-[a-z]+\d+$/,
  azure: /^[a-z]+\d*$/,
};
export const REGION_EXAMPLE: Record<Cloud, string> = {
  aws: "ap-northeast-2",
  gcp: "asia-northeast3",
  azure: "koreacentral",
};

/** schemas.single_line: 비어 있지 않고, 앞뒤 공백·줄바꿈이 없어야 합니다. */
export const isSingleLine = (value: string) =>
  value.length > 0 && value === value.trim() && !value.includes("\n") && !value.includes("\r");

export interface CloudStatus {
  cloud: Cloud;
  registered: boolean;
  required_keys: string[];
}

export interface RouteEnd {
  cloud: string;
  region: string;
}
export interface Route {
  source: RouteEnd;
  target: RouteEnd;
}

export interface RunSummary {
  run_id: string;
  route: Route | null;
  active: boolean;
}

export type RunState =
  | "pending"
  | "running"
  | "waiting_approval"
  | "paused"
  | "succeeded"
  | "failed"
  | "aborted"
  | "stopped"
  | "unknown";

export type CutoverOutcome = "not_started" | "rolled_back" | "switched" | "post_switch_failed" | "unknown";

export interface TableSync {
  source_rows: number;
  target_rows: number;
  percent?: number;
  match?: boolean;
}

export interface CutoverStepRecord {
  step: string;
  elapsed_ms?: number;
  waited_ms?: number;
  mismatch?: number;
  error?: string;
}

/** 값이 있는 키만 옵니다. 없는 키는 미제공입니다. */
export interface RunMetrics {
  phase?: string;
  simulated?: boolean;
  copy_percent?: number;
  tables?: Record<string, TableSync>;
  rows_checked?: number;
  mismatch?: number;
  measured_at?: string;
  measure_error?: string;
  replication_lag_bytes?: number;
  verification?: string;
  serving?: "source" | "target";
  target_db_network?: "PRIVATE" | "PUBLIC";
  cutover_outcome?: CutoverOutcome;
  cutover_seconds?: number;
  cutover_seconds_source?: "worker" | "agent";
  cutover_steps?: CutoverStepRecord[];
  cutover_sequence?: "VERIFIED" | "INCOMPLETE";
  cutover_sequence_missing?: string[];
  cdc_error?: string;
}

export interface MigrationRunStatus {
  run_id: string;
  job_id: string;
  route: Route | null;
  state: RunState;
  phase: string;
  step: number | null;
  last_step: number;
  progress: number;
  message: string | null;
  error: { code: string; message: string } | null;
  database_choices: { source: string[]; target: string[] } | null;
  metrics: RunMetrics;
  ready_for_cutover: boolean;
  cleanup_required: boolean;
  remaining: string[];
}

export interface CutoverResult {
  run_id: string;
  job_id: string;
  outcome: CutoverOutcome;
  state: string;
  phase: string;
  message: string;
  detail?: string | null;
  hold_ms?: number | null;
  metrics: RunMetrics;
}

export interface FinishResult {
  run_id: string;
  job_id: string;
  state: "finished" | "incomplete" | string;
  message?: string | null;
  vpn: "removed" | "kept" | string;
  remaining: string[];
  next_steps: string[];
}

export interface DbAccount {
  user: string;
  password: string;
  database?: string | null;
}
export interface RunInput {
  source: { cloud: Cloud; region: string; db: DbAccount };
  target: { cloud: Cloud; region: string; db: DbAccount & { app_user: string } };
}

// ---------------------------------------------------------------- 표시용 표

/** UI_API.md "단계". step 이 null 이면 전환 이후입니다. */
export const PHASE_LABEL: Record<string, string> = {
  inputs: "입력 확인·DB 조회·시작 전 검사",
  vpn: "VPN 구축",
  probe: "DB 접속·목록 확인",
  secrets: "계정 전달",
  job: "작업 등록",
  source_prepare: "원본 준비",
  target_prepare: "목적지 준비",
  precheck: "복제 전 검사",
  replication_setup: "복제 설정",
  snapshot: "초기 복사",
  streaming: "변경 복제 중",
  cutover_ready: "전환 승인 대기",
  cutover: "쓰기 전환",
  stabilizing: "전환 후 안정화",
  closed: "종료",
  unknown: "알 수 없음",
};

export const STATE_LABEL: Record<RunState, string> = {
  pending: "대기",
  running: "진행 중",
  waiting_approval: "승인 대기",
  paused: "일시 정지",
  succeeded: "완료",
  failed: "실패",
  aborted: "중단됨",
  stopped: "정리됨",
  unknown: "확인 불가",
};

/** UI_API.md "오류 코드". 모르는 코드는 message 만 보여 줍니다. */
const ERROR_GUIDE_GROUPS: [string[], string][] = [
  [["database_selection_required"], "DB 가 여러 개입니다. 아래에서 고른 뒤 다시 실행하세요."],
  [["invalid_database_selection", "no_database"], "DB 이름과 서버를 확인하세요."],
  [
    ["no_primary_key", "table_not_readable", "partitioned_table", "invalid_table_name", "no_source_tables"],
    "메시지에 나온 테이블을 고친 뒤 다시 실행하세요.",
  ],
  [["lookup_failed", "invalid_route"], "클라우드 권한과 DB 구성을 확인하세요."],
  [
    ["source_public_access", "cidr_overlap", "invalid_cidr", "missing_cidr", "node_outside_vcn", "invalid_source", "source_busy"],
    "원본 설정이나 네트워크를 고친 뒤 다시 실행하세요.",
  ],
  [
    [
      "agent_not_ready",
      "agent_unavailable",
      "invalid_agent_response",
      "preflight_failed",
      "agent_error",
      "agent_unreachable",
      "catalog_unavailable",
      "invalid_catalog_response",
    ],
    "잠시 뒤 다시 실행하세요. 반복되면 CDC 클러스터 운영자 확인이 필요합니다.",
  ],
  [
    ["tunnel_timeout", "terraform", "credentials", "peer_changed", "invalid_output", "invalid_plan", "route_mismatch", "unsupported_cloud"],
    "다시 실행하거나, 중단으로 VPN 을 정리하세요.",
  ],
  [
    ["context_changed", "legacy_context", "context_version", "invalid_context", "invalid_psk", "missing_psk", "cleanup_failed", "busy"],
    "중단으로 정리한 뒤 새 실행을 시작하세요.",
  ],
  [["db_unreachable"], "메시지의 주소와 라우팅을 확인한 뒤 다시 실행하세요."],
  [["missing_setting"], "백엔드 환경변수를 확인하세요."],
  [["job_failed"], "복제 작업이 실패했습니다. 메시지를 보고 중단 여부를 판단하세요."],
  [["status_unknown"], "에이전트와 통신하지 못했습니다. 다음 조회에서 다시 확인합니다."],
  [["internal", "stopped"], "메시지를 확인한 뒤 다시 실행하세요."],
];
const ERROR_GUIDE = new Map(ERROR_GUIDE_GROUPS.flatMap(([codes, text]) => codes.map((c) => [c, text] as const)));
export const errorGuide = (code: string): string | null => ERROR_GUIDE.get(code) ?? null;

/** remaining[] 의 항목 이름. secret:<tag> 는 실행용 비밀값입니다. */
export function remainingLabel(item: string): string {
  if (item === "agent_job") return "CDC 복제 작업";
  if (item === "vpn") return "VPN 터널";
  if (item.startsWith("secret:")) return `저장된 비밀값 (${item.slice("secret:".length)})`;
  return item;
}

const cloudName = (cloud: string) => CLOUD_LABEL[cloud as Cloud] ?? cloud;

export const routeText = (route: Route | null | undefined): string =>
  route
    ? `${cloudName(route.source.cloud)} ${route.source.region} → ${cloudName(route.target.cloud)} ${route.target.region}`
    : "경로 미제공";

/** 작업 등록 전에는 step/last_step, 등록 뒤에는 copy_percent. 둘 다 없으면 null (미제공). */
export function progressPercent(status: MigrationRunStatus): { value: number; basis: "copy" | "step" } | null {
  const copy = status.metrics.copy_percent;
  if (typeof copy === "number") return { value: Math.max(0, Math.min(100, copy)), basis: "copy" };
  if (typeof status.step === "number" && status.last_step > 0) {
    return { value: Math.round((status.step / status.last_step) * 100), basis: "step" };
  }
  return null;
}

const FINAL_STATES: ReadonlySet<RunState> = new Set(["succeeded", "failed", "aborted", "stopped"]);
/** 더 볼 것이 없는 상태. 정리가 남았으면 계속 봅니다. */
export const isSettled = (status: MigrationRunStatus) =>
  FINAL_STATES.has(status.state) && !status.cleanup_required;

/** 전환을 시도했거나 쓰기가 목적지로 갔으면 finish, 아니면 stop 으로 정리합니다. */
export function cleanupAction(status: MigrationRunStatus): "finish" | "stop" {
  const outcome = status.metrics.cutover_outcome;
  if (status.metrics.serving === "target") return "finish";
  if (outcome === "switched" || outcome === "post_switch_failed") return "finish";
  if (status.phase === "cutover" || status.phase === "stabilizing" || status.phase === "closed") return "finish";
  return "stop";
}

// ---------------------------------------------------------------- 호출

export class CdcApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly fields: string[] = [],
  ) {
    super(message);
  }
  /** 프록시 시간 초과·연결 끊김. 서버에서는 계속 진행 중일 수 있습니다. */
  get isTransport() {
    return this.status === 0 || this.status === 502 || this.status === 504;
  }
}

const BASE = "/api/db-migration";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...init?.headers },
      cache: "no-store",
    });
  } catch {
    throw new CdcApiError(0, "백엔드에 연결하지 못했습니다. 서버에서는 요청이 계속 진행 중일 수 있습니다.");
  }
  if (res.status === 204) return undefined as T;
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const obj = (body ?? {}) as { detail?: unknown; fields?: unknown };
    const fields = Array.isArray(obj.fields) ? obj.fields.filter((f): f is string => typeof f === "string") : [];
    const detail = typeof obj.detail === "string" ? obj.detail : `요청 실패 (HTTP ${res.status})`;
    throw new CdcApiError(res.status, detail, fields);
  }
  return body as T;
}

const post = <T>(path: string, body?: unknown) =>
  call<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const enc = encodeURIComponent;

export const cdcRuns = {
  listClouds: () => call<CloudStatus[]>("/clouds"),
  /** 값은 돌려받지 않습니다. 저장 뒤 화면에서도 지웁니다. */
  registerCloud: (cloud: Cloud, keys: Record<string, string>) =>
    call<CloudStatus>(`/clouds/${cloud}`, { method: "PUT", body: JSON.stringify(keys) }),
  deleteCloud: (cloud: Cloud) => call<void>(`/clouds/${cloud}`, { method: "DELETE" }),
  startRun: (input: RunInput) => post<{ run_id: string }>("/runs", input),
  listRuns: () => call<RunSummary[]>("/runs"),
  getRun: (runId: string) => call<MigrationRunStatus>(`/runs/${enc(runId)}`),
  retry: (runId: string, choice?: { source_database?: string; target_database?: string }) =>
    post<{ run_id: string }>(`/runs/${enc(runId)}/retry`, choice),
  cutover: (runId: string, approvedBy: string) =>
    post<CutoverResult>(`/runs/${enc(runId)}/cutover`, { approved_by: approvedBy }),
  finish: (runId: string) => post<FinishResult>(`/runs/${enc(runId)}/finish`),
  stop: (runId: string) => post<FinishResult>(`/runs/${enc(runId)}/stop`),
};

// ---------------------------------------------------------------- 최근 실행 (이 브라우저)

// GET /runs 는 끝난 실행을 빼고 돌려주므로, 이 브라우저에서 시작한 실행 id 와 경로만 남겨 둡니다.
// 비밀값은 담지 않습니다.
const RECENT_KEY = "cdc-recent-runs";
const RECENT_LIMIT = 20;

export function loadRecentRuns(): RunSummary[] {
  try {
    const raw = window.localStorage.getItem(RECENT_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (r): r is RunSummary => typeof r === "object" && r !== null && typeof (r as RunSummary).run_id === "string",
    );
  } catch {
    return [];
  }
}

export function rememberRun(runId: string, route: Route | null) {
  try {
    const rest = loadRecentRuns().filter((r) => r.run_id !== runId);
    const next = [{ run_id: runId, route, active: false }, ...rest].slice(0, RECENT_LIMIT);
    window.localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    // 저장소를 못 쓰면 최근 목록만 빠집니다.
  }
}
