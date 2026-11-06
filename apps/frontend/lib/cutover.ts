// 승인 ② 판정과 복귀 가능 범위. run 상태만으로 계산합니다. 값이 없으면 통과로 치지 않고 "unknown" 입니다.

import { METRIC_KEYS, numberMetric } from "./dashboard";
import type { RunStatus } from "./workflows";

// 임시 기준값입니다. 팀 합의 후 바꿉니다.
export const MAX_REPLICATION_LAG_BYTES = 1024 * 1024;

export type CheckStatus = "pass" | "fail" | "unknown";

export type ReadinessCheck = {
  id: string;
  title: string;
  status: CheckStatus;
  detail: string;
};

export type CutoverDecision = "go" | "needs_confirmation" | "no_go";

function metricCheck(
  id: string,
  title: string,
  value: number | null,
  isPass: (v: number) => boolean,
  describe: (v: number) => string,
): ReadinessCheck {
  if (value === null) return { id, title, status: "unknown", detail: "미제공" };
  return { id, title, status: isPass(value) ? "pass" : "fail", detail: describe(value) };
}

function appHealthCheck(run: RunStatus): ReadinessCheck {
  const base = { id: "app_health", title: "목적지 앱 상태" };
  const stage = run.stages.find((s) => s.stage === "app_deploy");
  // 수동 입력값은 실제 확인 결과가 아니므로 통과나 실패로 치지 않습니다.
  if (stage?.manual) return { ...base, status: "unknown", detail: "수동 입력이라 확인 안 됨" };
  const output = run.outputs.app_deploy as { healthy?: unknown } | undefined;
  if (output?.healthy === true) return { ...base, status: "pass", detail: "/health/ready 200" };
  if (output?.healthy === false) return { ...base, status: "fail", detail: "상태 확인 실패" };
  return { ...base, status: "unknown", detail: "미제공" };
}

export function cutoverChecks(run: RunStatus): ReadinessCheck[] {
  return [
    metricCheck(
      "copy",
      "초기 복사",
      numberMetric(run, METRIC_KEYS.copyPercent),
      (v) => v >= 100,
      (v) => `${v}%`,
    ),
    metricCheck(
      "lag",
      "복제 지연",
      numberMetric(run, METRIC_KEYS.replicationLagBytes),
      (v) => v <= MAX_REPLICATION_LAG_BYTES,
      (v) => `${v.toLocaleString("ko-KR")} bytes (기준 ${MAX_REPLICATION_LAG_BYTES.toLocaleString("ko-KR")} 이하, 임시)`,
    ),
    metricCheck(
      "mismatch",
      "불일치 테이블",
      numberMetric(run, METRIC_KEYS.mismatchTables),
      (v) => v === 0,
      (v) => `${v}개`,
    ),
    appHealthCheck(run),
  ];
}

/** 실패가 하나라도 있으면 막습니다. 미제공만 있으면 운영자의 명시적 확인이 있어야 엽니다. */
export function cutoverDecision(checks: ReadinessCheck[]): CutoverDecision {
  if (checks.some((c) => c.status === "fail")) return "no_go";
  return checks.some((c) => c.status === "unknown") ? "needs_confirmation" : "go";
}

export type RecoveryTone = "safe" | "active" | "warning";

export type RecoveryStatus = {
  title: string;
  detail: string;
  tone: RecoveryTone;
};

// 문구는 cdc_agent/jobs.py 의 전환 동작과 맞춥니다. 경로를 바꾸기 전 실패만 자동으로 원본에 돌아갑니다.
export function recoveryStatus(run: RunStatus): RecoveryStatus | null {
  const stage = run.stages.find((s) => s.stage === "cutover");
  const isWaiting = run.state === "waiting_approval" && run.waiting_gate === "cutover";
  if (!stage || (!isWaiting && !run.approvals.includes("cutover"))) return null;

  if (isWaiting) {
    return { title: "전환 전", detail: "트래픽은 아직 원본이 처리합니다. 이전을 중단할 수 있습니다.", tone: "safe" };
  }
  if (stage.state === "failed") {
    return stage.message?.includes("롤백")
      ? { title: "원본으로 복귀함", detail: "경로를 바꾸기 전에 실패해 원본이 쓰기를 다시 처리합니다.", tone: "safe" }
      : { title: "수동 확인 필요", detail: "자동으로 복귀하지 않습니다. 목적지에 새 쓰기가 있는지 먼저 확인하세요.", tone: "warning" };
  }
  if (stage.state === "succeeded" || numberMetric(run, METRIC_KEYS.cutoverSeconds) !== null) {
    return {
      title: "전환 완료 · 자동 복귀 없음",
      detail: "역방향 복제가 없어 원본으로 돌아가면 목적지의 새 쓰기를 잃을 수 있습니다.",
      tone: "warning",
    };
  }
  return { title: "전환 중", detail: "경로를 바꾸기 전에 실패하면 원본으로 자동 복귀합니다.", tone: "active" };
}
