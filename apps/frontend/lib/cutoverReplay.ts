// 실측 cutover_steps 재생. 에이전트(infra/db_migration/agent/cdc_agent/jobs.py)가 남긴 단계 이름과
// elapsed_ms(전환 시작부터 누적)만 씁니다. 단계 사이를 추정해 채우지 않습니다.

import type { CutoverStep } from "./dashboard";
import type { LiveTrafficPhase } from "./edgeLive";

export type ReplayPhase = LiveTrafficPhase | "failed";

export interface ReplayStep {
  step: string;
  elapsedMs: number;
}

// 보류가 걸린 뒤 목적지로 경로를 바꾸기 전까지의 단계입니다.
const HOLD_STEPS = new Set([
  "hold",
  "inflight_zero",
  "source_writes_blocked",
  "marker_on_target",
  "sink_paused",
  "sequences_synced",
  "snapshot_fixed",
  "validated", // 이전 에이전트가 snapshot_fixed 자리에 남기는 이름 (backend runners._CUTOVER_STEP_ALIASES)
]);

/** 한 단계가 게이트 상태를 어떻게 바꾸는지. 모르는 단계 이름은 이전 상태를 유지합니다(null). */
function phaseAfterStep(step: string): ReplayPhase | null {
  if (HOLD_STEPS.has(step)) return "hold";
  if (step === "switched_to_target") return "switch";
  if (step === "released") return "target";
  if (step === "failed") return "failed";
  return null;
}

/** 재생할 수 있는 단계(elapsed_ms 가 있는 것)와 빠진 단계 수. */
export function replayableSteps(steps: CutoverStep[] | null): { steps: ReplayStep[]; skipped: number } {
  if (!steps) return { steps: [], skipped: 0 };
  const usable = steps
    .filter((s): s is CutoverStep & { elapsed_ms: number } => typeof s.elapsed_ms === "number")
    .map((s) => ({ step: s.step, elapsedMs: s.elapsed_ms }));
  return { steps: usable, skipped: steps.length - usable.length };
}

/** index 번째 단계까지 재생했을 때의 게이트 상태. index 가 -1 이면 첫 단계 전(원본 처리)입니다. */
export function replayPhaseAt(steps: ReplayStep[], index: number): ReplayPhase {
  let phase: ReplayPhase = "source";
  for (let i = 0; i <= index && i < steps.length; i++) {
    phase = phaseAfterStep(steps[i].step) ?? phase;
  }
  return phase;
}
