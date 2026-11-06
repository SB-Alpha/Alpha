"use client";

import { useState } from "react";
import { GATE_LABEL } from "@/lib/labels";
import { BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SECONDARY, INPUT } from "@/lib/ui";
import type { RunStatus } from "@/lib/workflows";
import { ReadinessTable } from "./CutoverSafety";
import { ManualInputForm } from "./ManualInputForm";

type Props = {
  run: RunStatus;
  isBusy: boolean;
  onApproveCutover: (comment: string, acknowledgeUnknown: boolean) => void;
  onManual: (output: Record<string, unknown>, note: string) => void;
  onResume: () => void;
  onAbort: (reason: string) => void;
};

export function RunActions({ run, isBusy, onApproveCutover, onManual, onResume, onAbort }: Props) {
  const [cutoverComment, setCutoverComment] = useState("");
  const [isUnknownConfirmed, setIsUnknownConfirmed] = useState(false);

  const isFinished = run.state === "succeeded" || run.state === "aborted";
  const canManual = run.state === "paused" || run.state === "failed";
  const isWaitingCutover = run.state === "waiting_approval" && run.waiting_gate === "cutover";

  // 판정은 서버가 합니다. 승인 API 도 같은 규칙으로 다시 검사합니다.
  const readiness = run.cutover_readiness ?? null;
  const decision = readiness?.decision ?? null;
  const isUnknownAcknowledged = decision === "needs_confirmation" && isUnknownConfirmed;
  const canApproveCutover = decision === "go" || isUnknownAcknowledged;

  const handleAbort = () => {
    const reason = window.prompt("이 run 을 중단합니다. 되돌릴 수 없습니다. 사유를 적어 주세요 (선택)");
    if (reason !== null) onAbort(reason);
  };

  return (
    <section aria-labelledby="actions-heading" className="space-y-4">
      <h2 id="actions-heading" className="sr-only">
        운영자 조작
      </h2>

      {isWaitingCutover && (
        <div className="rounded-xl border-2 border-amber-400 bg-white p-5">
          <h3 className="text-lg font-semibold">{GATE_LABEL.cutover}</h3>
          <p className="mt-1 text-sm text-ink/70">
            승인하면 요청 보류 → 최종 반영 확인 → 경로 변경 → 보류 해제 순서로 전환합니다.
          </p>
          {readiness && <ReadinessTable checks={readiness.checks} />}
          {decision === "no_go" && (
            <p role="alert" className="mt-2 text-sm font-medium text-rose-700">
              실패한 항목이 있어 전환을 승인할 수 없습니다.
            </p>
          )}
          {decision === "needs_confirmation" && (
            <label className="mt-2 flex items-start gap-2 text-sm text-amber-900">
              <input
                type="checkbox"
                checked={isUnknownConfirmed}
                onChange={(e) => setIsUnknownConfirmed(e.target.checked)}
                className="mt-0.5"
              />
              확인되지 않은 항목이 있음을 알고 진행합니다.
            </label>
          )}
          <div className="mt-3 flex flex-col gap-2 sm:flex-row">
            <input
              aria-label="전환 승인 메모"
              value={cutoverComment}
              onChange={(e) => setCutoverComment(e.target.value)}
              placeholder="메모 (선택)"
              className={INPUT}
            />
            <button
              type="button"
              disabled={isBusy || !canApproveCutover}
              onClick={() => onApproveCutover(cutoverComment, isUnknownAcknowledged)}
              className={`${BUTTON_PRIMARY} shrink-0`}
            >
              전환 승인
            </button>
          </div>
        </div>
      )}

      {canManual && run.current_stage && (
        <ManualInputForm key={run.current_stage} stage={run.current_stage} isBusy={isBusy} onSubmit={onManual} />
      )}

      {!isFinished && (
        <div className="flex flex-wrap gap-2">
          {run.state === "failed" && (
            <button type="button" disabled={isBusy} onClick={onResume} className={BUTTON_SECONDARY}>
              실패 단계 다시 시도
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
