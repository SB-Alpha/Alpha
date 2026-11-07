import { CutoverSimulationWidget } from "./3d/CutoverSimulationWidget";
import {
  METRIC_KEYS,
  cutoverApprovals,
  cutoverSecondsSource,
  cutoverSequence,
  cutoverSteps,
  numberMetric,
  type CutoverSequence,
  type CutoverStep,
} from "@/lib/dashboard";
import { CUTOVER_SECONDS_SOURCE_LABEL, PREFLIGHT_VERDICT_LABEL, formatTime } from "@/lib/labels";
import type { ApprovalRecord, CutoverDecision, RunStatus } from "@/lib/workflows";

type Props = { run: RunStatus };

const MISSING = "미제공";

const formatMs = (ms: number) => `${ms.toLocaleString("ko-KR")}ms`;

// 판정과 시각은 승인자 이름처럼 화면이 보고한 값입니다. 서버가 다시 재지 않으므로 "표시된" 값으로만 적습니다.
function displayedPreflightText(record: ApprovalRecord): string {
  if (!record.preflight_verdict) return "승인 화면의 점검 결과 없음";
  const measuredAt = record.preflight_measured_at ? ` · 측정 ${formatTime(record.preflight_measured_at)}` : "";
  return `승인 화면에 표시된 판정: ${PREFLIGHT_VERDICT_LABEL[record.preflight_verdict]}${measuredAt}`;
}

// 서버가 승인 시점에 계산한 값입니다. 옛 기록에는 없습니다.
const READINESS_DECISION_TEXT: Record<CutoverDecision, string> = {
  go: "승인 조건: 모두 통과",
  needs_confirmation: "승인 조건: 미확인 항목을 확인하고 승인",
  no_go: "승인 조건: 실패 항목 있음",
};

function ApprovalRow({ record }: { record: ApprovalRecord }) {
  return (
    <li className="py-2 text-sm">
      <p className="break-words">
        <span className="font-medium">{record.approver ? `${record.approver} (직접 입력)` : "이름 미기재"}</span>
        <span className="text-ink/55"> · {formatTime(record.at)}</span>
      </p>
      {record.readiness_decision && (
        <p className="mt-0.5 text-xs text-ink/70">{READINESS_DECISION_TEXT[record.readiness_decision]}</p>
      )}
      <p className="mt-0.5 text-xs text-ink/70">{displayedPreflightText(record)}</p>
      {record.comment && <p className="mt-0.5 break-words text-xs text-ink/55">메모: {record.comment}</p>}
    </li>
  );
}

function StepRow({ step, index }: { step: CutoverStep; index: number }) {
  const hasMismatch = step.mismatch !== undefined && step.mismatch > 0;
  return (
    <li className="py-2 text-sm">
      <div className="flex flex-wrap justify-between gap-x-3">
        <span className="min-w-0 break-all font-mono">
          <span className="text-ink/35">{index + 1}</span> {step.step}
        </span>
        <span className="flex flex-wrap gap-x-2 font-mono text-xs tabular-nums text-ink/60">
          {step.elapsed_ms !== undefined && <span>경과 {formatMs(step.elapsed_ms)}</span>}
          {step.waited_ms !== undefined && <span>대기 {formatMs(step.waited_ms)}</span>}
          {step.mismatch !== undefined && (
            <span className={hasMismatch ? "font-semibold text-rose-700" : undefined}>
              불일치 {step.mismatch.toLocaleString("ko-KR")}
            </span>
          )}
        </span>
      </div>
      {step.error && <p className="mt-1 break-words text-xs font-medium text-rose-700">오류: {step.error}</p>}
    </li>
  );
}

function CutoverSecondsResult({ run }: Props) {
  const seconds = numberMetric(run, METRIC_KEYS.cutoverSeconds);
  const source = cutoverSecondsSource(run);
  if (seconds === null || source === null) return null;
  return (
    <>
      <h3 className="mt-5 text-xs font-semibold tracking-wide text-ink/50">전환 시간</h3>
      <p className="mt-2 text-sm">
        <span className="font-mono tabular-nums">{seconds.toFixed(1)}초</span>
        <span className="text-ink/55"> · {CUTOVER_SECONDS_SOURCE_LABEL[source]}</span>
      </p>
    </>
  );
}

function StepList({ steps }: { steps: CutoverStep[] | null }) {
  if (steps === null) return <p className="mt-2 text-sm text-ink/50">{MISSING}</p>;
  if (steps.length === 0) return <p className="mt-2 text-sm text-ink/50">기록된 단계 없음</p>;
  return (
    <ol className="mt-1 divide-y divide-ink/10">
      {steps.map((step, i) => (
        <StepRow key={`${i}-${step.step}`} step={step} index={i} />
      ))}
    </ol>
  );
}

function SequenceResult({ sequence }: { sequence: CutoverSequence | null }) {
  if (sequence === null) return <p className="mt-2 text-sm text-ink/50">{MISSING}</p>;
  if (sequence.status === "VERIFIED") {
    return <p className="mt-2 text-sm font-medium text-emerald-700">VERIFIED · 순서 확인됨</p>;
  }
  return (
    <p className="mt-2 break-words text-sm font-medium text-amber-800">
      INCOMPLETE · 빠진 단계: {sequence.missing.length > 0 ? sequence.missing.join(", ") : MISSING}
    </p>
  );
}

export function CutoverTimeline({ run }: Props) {
  const steps = cutoverSteps(run);
  const approvals = cutoverApprovals(run);
  if (steps === null && approvals.length === 0) return null;

  const isWaitingCutover = run.state === "waiting_approval" && run.waiting_gate === "cutover";

  return (
    <section
      id="cutover-timeline-section"
      aria-labelledby="cutover-timeline-heading"
      className="rounded-xl border border-ink/15 bg-white p-5"
    >
      <h2 id="cutover-timeline-heading" className="text-lg font-semibold">
        전환 기록
      </h2>

      <div className="mt-4 grid gap-6 md:grid-cols-[2fr_3fr]">
        <div className="min-w-0">
          <h3 className="text-xs font-semibold tracking-wide text-ink/50">승인 ② 기록</h3>
          {approvals.length === 0 ? (
            <p className="mt-2 text-sm text-ink/50">{MISSING}</p>
          ) : (
            <ul className="mt-1 divide-y divide-ink/10">
              {approvals.map((record, i) => (
                <ApprovalRow key={`${record.at}-${i}`} record={record} />
              ))}
            </ul>
          )}
          <h3 className="mt-5 text-xs font-semibold tracking-wide text-ink/50">순서 검증</h3>
          <SequenceResult sequence={cutoverSequence(run)} />
          <CutoverSecondsResult run={run} />
        </div>
        <div className="min-w-0">
          <h3 className="text-xs font-semibold tracking-wide text-ink/50">전환 단계</h3>
          <StepList steps={steps} />
        </div>
      </div>

      {!isWaitingCutover && (
        <CutoverSimulationWidget
          run={run}
          defaultOpen={true}
          title="전환 트래픽: 실시간 Worker 상태 · 실측 기록 재생 & 파이프라인 주행"
        />
      )}
    </section>
  );
}
