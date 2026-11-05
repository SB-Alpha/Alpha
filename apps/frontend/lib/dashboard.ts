// 대시보드 계산. run 상태만으로 만들고, 값이 없으면 null(화면에 "미제공")을 돌려줍니다.
// db_migration 키는 #30·#31 이 실제로 보내는 이름입니다. serving·failed_requests·cutover_seconds 와
// 보안 키는 보내는 모듈이 아직 없어 "미제공"으로 남습니다. 어느 단계에서 보고하든 화면에 나타납니다.

import type { RunStatus, StageStatus, WorkflowState } from "./workflows";

export const METRIC_KEYS = {
  serving: "serving", // "aws" | "gcp": 지금 요청을 처리하는 쪽
  replicationLagBytes: "replication_lag_bytes",
  copyPercent: "copy_percent",
  tables: "tables", // { 테이블명: { source_rows, target_rows, percent, match } }
  mismatchTables: "mismatch", // 불일치 테이블 수
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

/** 뒤 단계부터 찾습니다. 같은 키를 여러 단계가 보고하면 가장 최근 단계 값을 씁니다. */
export function findMetric(run: RunStatus, key: string): unknown {
  for (const stage of [...run.stages].reverse()) {
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

const SECURITY_GOOD = ["PASS", "SECURE", "ENCRYPTED"];
const SECURITY_BAD = ["FAIL", "PUBLIC", "PLAINTEXT"];

export type SecurityTone = "neutral" | "good" | "bad";

export function securityTone(value: string | null): SecurityTone {
  if (value === null) return "neutral";
  if (SECURITY_GOOD.includes(value)) return "good";
  return SECURITY_BAD.includes(value) ? "bad" : "neutral";
}

/** 모듈이 실제로 보고한 값만 돌려줍니다. 보고가 없으면 null 입니다. */
export function securityAuditSummary(run: RunStatus) {
  const iamAudit = stringMetric(run, METRIC_KEYS.iamAuditStatus);
  const cloudSql = stringMetric(run, METRIC_KEYS.cloudSqlPrivateIp);
  const secretEncryption = stringMetric(run, METRIC_KEYS.secretEncryption);
  const isCompliant = [iamAudit, cloudSql, secretEncryption].every((v) => securityTone(v) === "good");
  return { iamAudit, cloudSql, secretEncryption, isCompliant };
}

/** tables 지표의 행 수 합계. 보고 전이거나 형식이 다르면 null 입니다. */
export function tableRowTotals(run: RunStatus): { source: number; target: number } | null {
  const tables = findMetric(run, METRIC_KEYS.tables);
  if (typeof tables !== "object" || tables === null) return null;
  const rows = Object.values(tables as Record<string, { source_rows?: unknown; target_rows?: unknown }>);
  if (rows.length === 0) return null;
  if (!rows.every((r) => Number.isFinite(r?.source_rows) && Number.isFinite(r?.target_rows))) return null;
  return {
    source: rows.reduce((sum, r) => sum + (r.source_rows as number), 0),
    target: rows.reduce((sum, r) => sum + (r.target_rows as number), 0),
  };
}

export function formatBytes(bytes: number): string {
  let size = bytes;
  for (const unit of ["B", "KB", "MB"]) {
    if (size < 1024) return `${size.toFixed(0)}${unit}`;
    size /= 1024;
  }
  return `${size.toFixed(1)}GB`;
}

/** 처리 중인 쪽. 보고값만 쓰고, 보고가 없으면 추정하지 않습니다. */
export function servingSide(run: RunStatus): "aws" | "gcp" | null {
  const reported = findMetric(run, METRIC_KEYS.serving);
  return reported === "aws" || reported === "gcp" ? reported : null;
}

export function formatDuration(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
