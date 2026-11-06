// 전환 중·후 복귀 가능 범위 표시. 승인 ② 판정은 서버(workflows/readiness.py)가 합니다.

import { METRIC_KEYS, numberMetric } from "./dashboard";
import type { RunStatus } from "./workflows";

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
