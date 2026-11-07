// 전환 결과와 복귀 가능 범위. run 상태만으로 계산합니다. 승인 ② 판정은 서버(workflows/readiness.py)가 합니다.

import { METRIC_KEYS, numberMetric } from "./dashboard";
import type { RunStatus } from "./workflows";

export type RecoveryTone = "safe" | "active" | "warning";

export type RecoveryStatus = {
  title: string;
  detail: string;
  tone: RecoveryTone;
};

const MANUAL_CHECK: RecoveryStatus = {
  title: "수동 확인 필요",
  detail: "트래픽이 이미 목적지로 넘어갔을 수 있고 자동으로 복귀하지 않습니다. 목적지에 새 쓰기가 있는지 먼저 확인하세요.",
  tone: "warning",
};

/** 백엔드가 기록한 전환 결과입니다. 메시지 문구로 추측하지 않습니다. */
export function cutoverOutcome(run: RunStatus): unknown {
  return run.stages.find((s) => s.stage === "cutover")?.metrics[METRIC_KEYS.cutoverOutcome];
}

const isOnSourceOutcome = (outcome: unknown) => outcome === "rolled_back" || outcome === "not_started";

/**
 * 트래픽이 원본에 있다고 확인하지 못한 전환이면 true 입니다. 중단된 run 도 포함합니다.
 * 전환이 실제로 진행 중일 때는 결과가 아직 없는 것이므로, 완료된 run 과 함께 제외합니다.
 */
export function needsManualCheck(run: RunStatus): boolean {
  const stage = run.stages.find((s) => s.stage === "cutover");
  if (run.state === "succeeded" || (run.state === "running" && stage?.state === "running")) return false;
  const outcome = cutoverOutcome(run);
  const isOnSource = isOnSourceOutcome(outcome);
  if (stage?.state === "failed") return !isOnSource;
  return outcome !== undefined && !isOnSource;
}

/**
 * 복제를 지워도 트래픽을 잃지 않는다고 볼 수 있으면 true 입니다.
 * 서버의 clean_up_cdc 처럼 결과가 없으면 전환 전으로 봅니다. 다만 전환이 도는 중이거나 결과 없이 실패했다면 위치를 알 수 없습니다.
 */
export function isTrafficOnSource(run: RunStatus): boolean {
  const outcome = cutoverOutcome(run);
  if (outcome !== undefined && outcome !== null) return isOnSourceOutcome(outcome);
  const state = run.stages.find((s) => s.stage === "cutover")?.state;
  return state !== "running" && state !== "failed";
}

// 문구는 cdc_agent/jobs.py 의 전환 동작과 맞춥니다. 경로를 바꾸기 전 실패만 자동으로 원본에 돌아갑니다.
export function recoveryStatus(run: RunStatus): RecoveryStatus | null {
  const stage = run.stages.find((s) => s.stage === "cutover");
  const isWaiting = run.state === "waiting_approval" && run.waiting_gate === "cutover";
  // 트래픽이 넘어갔을 수 있으면 다른 조건과 상관없이 먼저 알립니다.
  if (needsManualCheck(run)) return MANUAL_CHECK;
  // 전환이 원본에서 실패하면 workflows 가 승인 ② 를 취소하므로, 실패는 승인 여부와 상관없이 보여줍니다.
  if (!stage || (!isWaiting && stage.state !== "failed" && !run.approvals.includes("cutover"))) return null;
  if (isWaiting) {
    return { title: "전환 전", detail: "트래픽은 아직 원본이 처리합니다. 이전을 중단할 수 있습니다.", tone: "safe" };
  }
  if (stage.state === "failed") {
    return cutoverOutcome(run) === "rolled_back"
      ? { title: "원본으로 복귀함", detail: "경로를 바꾸기 전에 실패해 원본이 쓰기를 다시 처리합니다.", tone: "safe" }
      : { title: "전환 시작 안 함", detail: "전환 요청이 시작되지 않아 원본이 계속 처리합니다.", tone: "safe" };
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
