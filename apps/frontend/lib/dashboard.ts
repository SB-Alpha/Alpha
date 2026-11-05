// 대시보드 계산. run 상태만으로 만들고, 값이 없으면 null(화면에 "미제공")을 돌려줍니다.
// 지표 키는 아직 계약이 없어 제안 이름입니다. 모듈이 ctx.report(metrics={...})로 같은 이름을 보고하면
// 어느 단계에서 왔든 화면에 나타납니다 (확정서 3절 frontend 필수 지표).

import type { RunStatus, StageStatus, WorkflowState } from "./workflows";

export const METRIC_KEYS = {
  serving: "serving", // "aws" | "gcp": 지금 요청을 처리하는 쪽
  replicationLagSeconds: "replication_lag_seconds",
  rowsSource: "rows_source",
  rowsTarget: "rows_target",
  mismatchCount: "mismatch_count",
  failedRequests: "failed_requests",
  cutoverSeconds: "cutover_seconds", // T1 − T0
  iamAuditStatus: "iam_audit_status", // "PASS" | "WARN" | "FAIL"
  cloudSqlPrivateIp: "cloud_sql_private_ip", // "SECURE" | "PUBLIC"
  secretEncryption: "secret_encryption", // "ENCRYPTED" | "PLAINTEXT"
} as const;

const FINISHED: WorkflowState[] = ["succeeded", "failed", "aborted"];

export function isFinished(state: WorkflowState): boolean {
  return FINISHED.includes(state);
}

function stageProgress(stage: StageStatus): number {
  return stage.state === "succeeded" ? 100 : Math.min(100, Math.max(0, stage.progress));
}

/** 전체 진행률(0~100). 단계마다 같은 비중으로 평균합니다. */
export function overallProgress(run: RunStatus): number {
  if (run.stages.length === 0) return 0;
  const sum = run.stages.reduce((acc, s) => acc + stageProgress(s), 0);
  return Math.round(sum / run.stages.length);
}

export function stageCounts(run: RunStatus) {
  return {
    done: run.stages.filter((s) => s.state === "succeeded").length,
    manual: run.stages.filter((s) => s.manual).length,
    total: run.stages.length,
  };
}

/** run 시작부터 경과 초. 끝난 run은 마지막 갱신 시각에서 멈춥니다. */
export function elapsedSeconds(run: RunStatus, nowMs: number): number {
  const start = Date.parse(run.created_at);
  const end = isFinished(run.state) ? Date.parse(run.updated_at) : nowMs;
  return Math.max(0, Math.floor((end - start) / 1000));
}

/** 단계 순서대로 찾아 처음 나온 값을 씁니다. 어느 모듈이 보고하든 같은 칸에 표시됩니다. */
export function findMetric(run: RunStatus, key: string): unknown {
  for (const stage of run.stages) {
    if (key in stage.metrics) return stage.metrics[key];
  }
  return null;
}

export function numberMetric(run: RunStatus, key: string): number | null {
  const value = findMetric(run, key);
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) return Number(value);
  return null;
}

export function stringMetric(run: RunStatus, key: string): string | null {
  const value = findMetric(run, key);
  if (typeof value === "string" && value.trim() !== "") return value.trim();
  return null;
}

export function securityAuditSummary(run: RunStatus) {
  const iam = stringMetric(run, METRIC_KEYS.iamAuditStatus);
  const sql = stringMetric(run, METRIC_KEYS.cloudSqlPrivateIp);
  const secret = stringMetric(run, METRIC_KEYS.secretEncryption);

  const hasPlan = Boolean(run.outputs.planning);
  const hasProvisioning = Boolean(run.outputs.provisioning);

  return {
    iamAudit: iam ?? (hasPlan ? "PASS" : null),
    cloudSql: sql ?? (hasProvisioning ? "사설망 (VPC)" : hasPlan ? "사설망 예정" : null),
    secretManager: secret ?? (hasPlan ? "Secret Manager" : null),
  };
}

export type ServingSide = { side: "aws" | "gcp"; isReported: boolean };

/** 처리 중인 쪽. 보고값이 있으면 그대로, 없으면 전환 단계 완료 여부로만 판단합니다. */
export function servingSide(run: RunStatus): ServingSide {
  const reported = findMetric(run, METRIC_KEYS.serving);
  if (reported === "aws" || reported === "gcp") return { side: reported, isReported: true };
  const cutover = run.stages.find((s) => s.stage === "cutover");
  return { side: cutover?.state === "succeeded" ? "gcp" : "aws", isReported: false };
}

export function formatDuration(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
