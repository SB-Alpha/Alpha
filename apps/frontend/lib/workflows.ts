// workflows API 클라이언트. 타입은 backend `app/features/workflows/schemas.py` 와 같은 모양입니다.
// 요청은 Next.js rewrite(`/api/*` → WORKFLOWS_API_URL)를 거쳐 백엔드로 갑니다.

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

export interface MigrationRequest {
  github_repo_url: string;
  aws_region: string;
  aws_role_ref?: string | null;
  aws_credentials_ref?: string | null;
  ecs_service?: string | null;
  rds_instance_id?: string | null;
  db_credentials_ref?: string | null;
  db_admin_ref?: string | null;
  target_region?: string;
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

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
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
  approve: (runId: string, gate: Gate, comment?: string) =>
    post<RunStatus>(`/${encodeURIComponent(runId)}/approve`, { gate, comment: comment || null }),
  abort: (runId: string, reason?: string) =>
    post<RunStatus>(`/${encodeURIComponent(runId)}/abort`, { reason: reason || null }),
  resume: (runId: string) => post<RunStatus>(`/${encodeURIComponent(runId)}/resume`),
  submitManual: (runId: string, stage: StageName, output: Record<string, unknown>, note?: string) =>
    post<RunStatus>(`/${encodeURIComponent(runId)}/stages/${stage}/manual`, { output, note: note || null }),
};
