"use client";

import { cutoverOutcome, needsManualCheck } from "@/lib/cutover";
import { GATE_LABEL } from "@/lib/labels";
import { BUTTON_DANGER, BUTTON_SECONDARY } from "@/lib/ui";
import { GATE_BEFORE, type DisplayedPreflight, type RunStatus } from "@/lib/workflows";
import { CutoverApprovalPanel } from "./CutoverApprovalPanel";
import { ManualInputForm } from "./ManualInputForm";

type Props = {
  run: RunStatus;
  isBusy: boolean;
  onApproveCutover: (
    comment: string,
    approver: string,
    preflight: DisplayedPreflight,
    acknowledgeUnknown: boolean,
  ) => Promise<boolean | void> | boolean | void;
  onManual: (output: Record<string, unknown>, note: string) => void;
  onResume: () => void;
  onRetryCleanup: () => void;
  onAbort: (reason: string) => void;
};

export function RunActions({ run, isBusy, onApproveCutover, onManual, onResume, onRetryCleanup, onAbort }: Props) {
  const isFinished = run.state === "succeeded" || run.state === "aborted";
  const requiredGate = run.current_stage ? GATE_BEFORE[run.current_stage] : undefined;
  // 승인이 없는 단계의 수동 입력은 백엔드가 항상 거절합니다. 승인이 남아 있으면 결과 불명 전환을 완료로 기록하는 길입니다.
  const isManualRefused = requiredGate !== undefined && !run.approvals.includes(requiredGate);
  const isWaitingCutover = run.state === "waiting_approval" && run.waiting_gate === "cutover";
  const isCutoverFailed = run.state === "failed" && run.stages.some((s) => s.stage === "cutover" && s.state === "failed");
  const isTrafficUncertain = needsManualCheck(run);
  // 결과를 모르는 전환은 승인 조건이 막아도 사람이 확인한 결과를 기록할 수 있습니다. 전환을 다시 요청하지 않습니다.
  const canRecordUncertain = isWaitingCutover && isTrafficUncertain;
  const canManual = canRecordUncertain || ((run.state === "paused" || run.state === "failed") && !isManualRefused);
  // 전환 후 오류는 에이전트가 다시 받지 않으므로 재시도할 수 없습니다. 수동 입력이나 중단만 남습니다.
  const canResume = run.state === "failed" && !(isCutoverFailed && cutoverOutcome(run) === "post_switch_failed");

  const handleAbort = () => {
    const risk = isTrafficUncertain ? "트래픽이 이미 목적지로 넘어갔을 수 있으니 먼저 확인하세요. " : "";
    const reason = window.prompt(`${risk}이 run 을 중단합니다. 되돌릴 수 없습니다. 사유를 적어 주세요 (선택)`);
    if (reason !== null) onAbort(reason);
  };

  return (
    <section aria-labelledby="actions-heading" className="space-y-4">
      <h2 id="actions-heading" className="sr-only">
        운영자 조작
      </h2>

      {isWaitingCutover && (
        <CutoverApprovalPanel
          key={run.run_id}
          run={run}
          runId={run.run_id}
          readiness={run.cutover_readiness ?? null}
          isBusy={isBusy}
          isTrafficUncertain={isTrafficUncertain}
          onApprove={onApproveCutover}
        />
      )}

      {run.cleanup_message && (
        <div
          role={run.cleanup_state === "failed" ? "alert" : "status"}
          className={`rounded-lg border p-3 text-sm ${
            run.cleanup_state === "failed"
              ? "border-rose-300 bg-rose-50 text-rose-900"
              : "border-ink/15 bg-stone-50 text-ink/70"
          }`}
        >
          <p>{run.cleanup_message}</p>
          {run.cleanup_state === "failed" && (
            <button type="button" disabled={isBusy} onClick={onRetryCleanup} className={`${BUTTON_SECONDARY} mt-2`}>
              정리 다시 시도
            </button>
          )}
        </div>
      )}

      {canManual && run.current_stage && (
        <ManualInputForm key={run.current_stage} stage={run.current_stage} isBusy={isBusy} onSubmit={onManual} />
      )}

      {!isFinished && (
        <div className="flex flex-wrap gap-2">
          {isCutoverFailed && (
            <p className="w-full text-sm text-amber-900">
              {isTrafficUncertain
                ? "수동 확인 필요: 다시 시도를 권하지 않습니다. 에이전트에서 실제 상태를 확인하고, 전환이 끝났으면 수동 입력으로 완료를 기록하세요."
                : `다시 시도하려면 ${GATE_LABEL.cutover}을 다시 받아야 합니다.`}
            </p>
          )}
          {canResume && (
            <button type="button" disabled={isBusy} onClick={onResume} className={BUTTON_SECONDARY}>
              {isTrafficUncertain ? `상태 확인 후 ${GATE_LABEL.cutover} 다시 받기` : "실패 단계 다시 시도"}
            </button>
          )}
          <button type="button" disabled={isBusy} onClick={handleAbort} className={BUTTON_DANGER}>
            이전 중단
          </button>
        </div>
      )}
    </section>
  );
}
