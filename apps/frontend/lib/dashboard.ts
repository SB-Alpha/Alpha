// 대시보드 계산. run 상태만으로 만들고, 값이 없으면 null(화면에 "미제공")을 돌려줍니다.
// db_migration 키는 #30·#31 이 실제로 보내는 이름입니다. serving 은 workflows 가 에이전트 보고로, cutover_seconds 는
// 워커 측정값이나 에이전트 보고로 채웁니다(cutover_seconds_source). failed_requests·probe_* 는 프로버 실측값입니다.
// 실제 run 에서 ALPHA_PROBE_URL 이 없으면 "미제공"이고, 시뮬레이터는 failed_requests 를 자체 값으로 보냅니다.
// 목적지 DB 네트워크는 db_migration 계약(UI_API.md)의 target_db_network 를 읽고, 예전 run 의 cloud_sql_private_ip 도 읽습니다.

import { isRecord } from "./plan";
import type { ApprovalRecord, PreflightReport, PreflightVerdict, RunStatus, StageStatus, WorkflowState } from "./workflows";

export const METRIC_KEYS = {
  simulated: "simulated", // true: 실제 에이전트가 아닌 시뮬레이션 값
  serving: "serving", // "source" | "target": 지금 쓰기를 처리하는 쪽 (예전 run 은 "aws" | "gcp")
  replicationLagBytes: "replication_lag_bytes",
  copyPercent: "copy_percent",
  tables: "tables", // { 테이블명: { source_rows, target_rows, percent, match } }
  mismatchTables: "mismatch", // 불일치 테이블 수. 전환 전에는 행 수만, 전환 시점 비교 뒤에는 내용까지 비교한 값입니다
  verification: "verification", // 전환 시점 데이터 비교: "running" | "match" | "mismatch" | "unverifiable" | "unknown"
  failedRequests: "failed_requests",
  probeRequests: "probe_requests", // 프로버가 보낸 요청 수
  probeMaxMs: "probe_max_ms", // 가장 오래 기다린 요청
  writeRequests: "write_requests", // 보류 시작부터 Worker 가 처리한 쓰기 요청 수
  writeFailedRequests: "write_failed_requests", // 그중 원본 5xx·연결 실패 수
  cutoverSeconds: "cutover_seconds", // T1 − T0
  cutoverSecondsSource: "cutover_seconds_source", // "worker" | "agent"
  cutoverSteps: "cutover_steps", // [{ step, elapsed_ms?, waited_ms?, mismatch?, error? }]
  cutoverSequence: "cutover_sequence", // "VERIFIED" | "INCOMPLETE"
  cutoverSequenceMissing: "cutover_sequence_missing", // INCOMPLETE 일 때 빠진 단계 이름
  cutoverOutcome: "cutover_outcome", // "not_started" | "rolled_back" | "unknown" | "post_switch_failed" | "switched"
  targetDbNetwork: "target_db_network", // "PRIVATE" | "PUBLIC": 목적지 DB 주소가 사설망인지
  legacyCloudSqlPrivateIp: "cloud_sql_private_ip", // 예전 run: "SECURE" | "PUBLIC"
  secretEncryption: "secret_encryption", // "ENCRYPTED" | "PLAINTEXT"
  ingressGuard: "ingress_guard", // 배포 직후 직접 호출을 실제로 쳐 본 결과: "SECURE" | "EXPOSED" | "MISCONFIGURED"
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

const SECURITY_GOOD = ["PASS", "SECURE", "ENCRYPTED", "PRIVATE"];
const SECURITY_BAD = ["FAIL", "PUBLIC", "PLAINTEXT", "EXPOSED", "MISCONFIGURED"];

export type SecurityTone = "neutral" | "good" | "bad";

export function securityTone(value: string | null): SecurityTone {
  if (value === null) return "neutral";
  if (SECURITY_GOOD.includes(value)) return "good";
  return SECURITY_BAD.includes(value) ? "bad" : "neutral";
}

/** 실패·불일치 건수의 색. 정확히 0 만 good 이고, 보고가 없으면 neutral 입니다. */
export function countTone(value: number | null): SecurityTone {
  if (value === 0) return "good";
  return value !== null && value > 0 ? "bad" : "neutral";
}

/** 시뮬레이션 모드에서 나온 값이 섞여 있으면 true 입니다. */
export function isSimulated(run: RunStatus): boolean {
  return findMetric(run, METRIC_KEYS.simulated) === true;
}

/** 모듈이 실제로 보고한 값만 돌려줍니다. 보고가 없으면 null 입니다. */
export function securityAuditSummary(run: RunStatus) {
  const targetDbNetwork = targetDbNetworkOf(run);
  const secretEncryption = stringMetric(run, METRIC_KEYS.secretEncryption);
  const ingressGuard = stringMetric(run, METRIC_KEYS.ingressGuard);
  const isCompliant = [targetDbNetwork, secretEncryption, ingressGuard].every((v) => securityTone(v) === "good");
  return { targetDbNetwork, secretEncryption, ingressGuard, isCompliant };
}

/** 목적지 DB 네트워크. 예전 run 의 cloud_sql_private_ip(SECURE) 는 PRIVATE 로 읽습니다. */
export function targetDbNetworkOf(run: RunStatus): "PRIVATE" | "PUBLIC" | null {
  const current = stringMetric(run, METRIC_KEYS.targetDbNetwork);
  if (current === "PRIVATE" || current === "PUBLIC") return current;
  const legacy = stringMetric(run, METRIC_KEYS.legacyCloudSqlPrivateIp);
  if (legacy === "SECURE") return "PRIVATE";
  return legacy === "PUBLIC" ? "PUBLIC" : null;
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

export interface CutoverStep {
  step: string;
  elapsed_ms?: number;
  waited_ms?: number;
  mismatch?: number;
  error?: string;
}

export type CutoverSecondsSource = "worker" | "agent";

const VERIFICATIONS = ["running", "match", "mismatch", "unverifiable", "unknown"] as const;
export type Verification = (typeof VERIFICATIONS)[number];

export interface CutoverSequence {
  status: "VERIFIED" | "INCOMPLETE";
  missing: string[];
}

const finiteNumber = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

function readCutoverStep(raw: unknown): CutoverStep | null {
  if (!isRecord(raw) || typeof raw.step !== "string" || raw.step.trim() === "") return null;
  return {
    step: raw.step,
    elapsed_ms: finiteNumber(raw.elapsed_ms),
    waited_ms: finiteNumber(raw.waited_ms),
    mismatch: finiteNumber(raw.mismatch),
    error: typeof raw.error === "string" && raw.error.trim() !== "" ? raw.error : undefined,
  };
}

/** 에이전트가 보고한 전환 단계. 형식이 어긋난 항목은 버립니다. 보고가 없으면 null, 보고했지만 비어 있으면 빈 목록입니다. */
export function cutoverSteps(run: RunStatus): CutoverStep[] | null {
  const raw = findMetric(run, METRIC_KEYS.cutoverSteps);
  if (!Array.isArray(raw)) return null;
  return (raw as unknown[]).map(readCutoverStep).filter((s): s is CutoverStep => s !== null);
}

/** 전환 시간을 잰 기준. 보고가 없거나 값이 다르면 null 입니다. */
export function cutoverSecondsSource(run: RunStatus): CutoverSecondsSource | null {
  const source = stringMetric(run, METRIC_KEYS.cutoverSecondsSource);
  return source === "worker" || source === "agent" ? source : null;
}

export interface CutoverOverlayFacts {
  simulated: boolean;
  cutoverSeconds: number | null;
  cutoverSecondsSource: CutoverSecondsSource | null;
  failedRequests: number | null;
  writeFailedRequests: number | null;
  steps: { step: string; elapsedMs: number | null }[] | null;
}

/** 전환 오버레이에 쓰는 이 run 의 보고값. elapsedMs 는 전환 스레드 시작부터 누적이며, 단계별 소요가 아닙니다. */
export function cutoverOverlayFacts(run: RunStatus): CutoverOverlayFacts {
  const steps = cutoverSteps(run);
  return {
    simulated: isSimulated(run),
    cutoverSeconds: numberMetric(run, METRIC_KEYS.cutoverSeconds),
    cutoverSecondsSource: cutoverSecondsSource(run),
    failedRequests: numberMetric(run, METRIC_KEYS.failedRequests),
    writeFailedRequests: numberMetric(run, METRIC_KEYS.writeFailedRequests),
    steps: steps && steps.map((s) => ({ step: s.step, elapsedMs: s.elapsed_ms ?? null })),
  };
}

/** 전환 시점 데이터 비교 상태. 전환 전이거나 보고가 없으면 null 입니다. */
export function verificationState(run: RunStatus): Verification | null {
  const value = stringMetric(run, METRIC_KEYS.verification);
  return VERIFICATIONS.find((v) => v === value) ?? null;
}

/** 전환 순서 검증 결과. 보고가 없거나 값이 다르면 null 입니다 (INCOMPLETE 로 추정하지 않음). */
export function cutoverSequence(run: RunStatus): CutoverSequence | null {
  const status = stringMetric(run, METRIC_KEYS.cutoverSequence);
  if (status !== "VERIFIED" && status !== "INCOMPLETE") return null;
  const missing = findMetric(run, METRIC_KEYS.cutoverSequenceMissing);
  return {
    status,
    missing: Array.isArray(missing) ? (missing as unknown[]).filter((s): s is string => typeof s === "string") : [],
  };
}

/** 승인 ② 기록. 재시도하면 여러 건입니다. 이 필드가 없는 응답도 빈 목록으로 읽습니다. */
export function cutoverApprovals(run: RunStatus): ApprovalRecord[] {
  return (run.approval_records ?? []).filter((r) => r.gate === "cutover");
}

const VERDICTS: PreflightVerdict[] = ["go", "no_go", "unverified"];

/** 화면에 쓸 판정. 시뮬레이션이거나 모르는 값이면 go 로 보이지 않게 unverified 로 둡니다. */
export function preflightVerdict(report: PreflightReport): PreflightVerdict {
  if (report.simulated || !VERDICTS.includes(report.verdict)) return "unverified";
  return report.verdict;
}

export function formatBytes(bytes: number): string {
  let size = bytes;
  for (const unit of ["B", "KB", "MB"]) {
    if (size < 1024) return `${size.toFixed(0)}${unit}`;
    size /= 1024;
  }
  return `${size.toFixed(1)}GB`;
}

export type ServingSide = "source" | "target";

/** 전환 승인 ② 의 복제 지연 기준. workflows/readiness.py MAX_REPLICATION_LAG_BYTES 와 같은 값입니다. */
export const MAX_SAFE_LAG_BYTES = 1024 * 1024;

/** 쓰기를 처리하는 쪽. 보고값만 쓰고, 보고가 없으면 추정하지 않습니다.
 * db_migration 계약은 source|target 입니다. 예전 run 에 저장된 aws(원본)·gcp(목적지)도 읽습니다. */
export function servingSide(run: RunStatus): ServingSide | null {
  const reported = findMetric(run, METRIC_KEYS.serving);
  if (reported === "source" || reported === "target") return reported;
  if (reported === "aws") return "source";
  return reported === "gcp" ? "target" : null;
}

export const SERVING_LABEL: Record<ServingSide, string> = { source: "원본", target: "목적지" };

export function formatDuration(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
