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
  onDecommission: () => void;
  onAbort: (reason: string) => void;
};

// 정리하지 않으면 남는 자원. 금액은 가격 API 로 계산하지 않아 적지 않습니다 (미제공).
const LEFTOVER_RESOURCES = [
  "원본·목적지 클라우드의 VPN 게이트웨이와 OCI CDC 노드로 가는 IPsec 터널 (시간 단위 과금)",
  "CDC 복제 작업 (OCI 의 Kafka·Debezium 커넥터)",
  "원본 DB 복제 슬롯 (WAL 이 계속 쌓여 원본 디스크를 채움)",
];

function DecommissionPanel({ run, isBusy, onDecommission }: { run: RunStatus; isBusy: boolean; onDecommission: () => void }) {
  const handleClick = () => {
    const ok = window.confirm(
      "복제 작업을 닫고 VPN 을 지웁니다. 이후에는 원본 DB 로 되돌아갈 수 없습니다. 목적지에서 서비스가 정상인지 확인했나요?",
    );
    if (ok) onDecommission();
  };
  return (
    <div className="rounded-lg border border-ink/20 bg-white p-3 text-sm">
      <p className="font-semibold text-ink">이전 자원 정리</p>
      <p className="mt-1 text-xs text-ink/70">전환이 끝났습니다. 아래 자원은 지우기 전까지 매달 비용이 나갑니다 (금액 미제공).</p>
      <ul className="mt-1 list-disc pl-4 text-xs text-ink/70">
        {LEFTOVER_RESOURCES.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      <button type="button" disabled={isBusy} onClick={handleClick} className={`${BUTTON_SECONDARY} mt-2`}>
        {run.cleanup_state === "failed" ? "이전 자원 정리 다시 시도" : "이전 자원 정리"}
      </button>
    </div>
  );
}

export function RunActions({
  run,
  isBusy,
  onApproveCutover,
  onManual,
  onResume,
  onRetryCleanup,
  onDecommission,
  onAbort,
}: Props) {
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
  // 전환이 끝난 run 은 정리 전이거나 정리에 실패했을 때 정리 버튼을 보여 줍니다. 중단된 run 의 정리는 아래 기존 경로입니다.
  const canDecommission = run.state === "succeeded" && (run.cleanup_state == null || run.cleanup_state === "failed");

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
          {run.cleanup_state === "failed" && run.state === "aborted" && (
            <button type="button" disabled={isBusy} onClick={onRetryCleanup} className={`${BUTTON_SECONDARY} mt-2`}>
              정리 다시 시도
            </button>
          )}
        </div>
      )}

      {canDecommission && <DecommissionPanel run={run} isBusy={isBusy} onDecommission={onDecommission} />}

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
