import { GATE_LABEL, STAGE_LABEL } from "@/lib/labels";
import { formatTimingSeconds, useRunTimings } from "@/lib/timings";
import { GATE_BEFORE, type Gate, type RunStatus } from "@/lib/workflows";
import { isFinished } from "@/lib/dashboard";
import { StateBadge } from "./StateBadge";

type Props = { run: RunStatus };

function gateTone(run: RunStatus, gate: Gate): string {
  if (run.approvals.includes(gate)) return "text-emerald-700";
  if (run.waiting_gate === gate) return "text-amber-700";
  return "text-ink/40";
}

export function StageStepper({ run }: Props) {
  const { timings, baseline } = useRunTimings(run.run_id, !isFinished(run.state));

  return (
    <div className="space-y-2">
      {timings?.measured_total_seconds !== null && timings?.measured_total_seconds !== undefined && (
        <div className="flex flex-wrap items-center justify-between rounded-lg border border-ink/10 bg-paper/60 px-3 py-1.5 text-xs font-mono">
          <span className="text-ink/60">
            실측 소요 시간 합계: <strong className="text-ink font-semibold">{formatTimingSeconds(timings.measured_total_seconds)}</strong>
            {timings.approval_wait_seconds !== null && (
              <span className="ml-2 text-ink/45">(승인 대기: {formatTimingSeconds(timings.approval_wait_seconds)})</span>
            )}
          </span>
          {baseline && baseline.runs_considered > 0 && (
            <span className="text-[11px] text-ink/50">
              ※ 과거 실측 {baseline.runs_considered}회 기준 통계 (참고용)
            </span>
          )}
        </div>
      )}

      <ol className="grid gap-2 sm:grid-cols-2 lg:grid-cols-6" aria-label="이전 단계">
        {run.stages.map((s, i) => {
          const gate = GATE_BEFORE[s.stage];
          const isCurrent = run.current_stage === s.stage;
          const stageTiming = timings?.stages.find((t) => t.stage === s.stage);
          const stageBase = baseline?.stages.find((b) => b.stage === s.stage);

          // tables 같은 객체 값은 위 대시보드에서 합계로 보여 줍니다.
          const metrics = Object.entries(s.metrics).filter(([, value]) => typeof value !== "object");
          return (
            <li
              key={s.stage}
              aria-current={isCurrent ? "step" : undefined}
              className={`rounded-lg border bg-white p-3 flex flex-col justify-between ${
                isCurrent ? "border-ink shadow-[3px_3px_0_0_var(--color-ink)]" : "border-ink/15"
              }`}
            >
              <div>
                <p className={`h-4 font-mono text-[10px] tracking-wide ${gate ? gateTone(run, gate) : ""}`}>
                  {gate ? `${run.approvals.includes(gate) ? "✓" : "◆"} ${GATE_LABEL[gate]}` : ""}
                </p>
                <p className="mt-1 font-mono text-[11px] text-ink/40">{String(i + 1).padStart(2, "0")}</p>
                <p className="font-medium text-sm">{STAGE_LABEL[s.stage]}</p>
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  <StateBadge state={s.state} />
                  {s.manual && (
                    <span className="rounded bg-ink px-1.5 py-0.5 text-[10px] font-semibold text-paper">수동</span>
                  )}
                </div>
                <div
                  className="mt-2 h-1 overflow-hidden rounded bg-ink/10"
                  role="progressbar"
                  aria-valuenow={s.progress}
                  aria-valuemin={0}
                  aria-valuemax={100}
                >
                  <div
                    className="h-full w-full origin-left bg-ink/70 transition-transform duration-500"
                    style={{ transform: `scaleX(${s.progress / 100})` }}
                  />
                </div>
                {s.message && <p className="mt-2 text-xs text-ink/70">{s.message}</p>}

                {/* Juho PR #92 실측 소요 시간 연동 */}
                {stageTiming?.seconds !== null && stageTiming?.seconds !== undefined ? (
                  <div className="mt-2 rounded bg-ink/[0.04] p-1.5 font-mono text-[11px]">
                    <div className="flex justify-between text-ink/70">
                      <span>실측 소요</span>
                      <span className="font-semibold text-ink">{formatTimingSeconds(stageTiming.seconds)}</span>
                    </div>
                    {stageBase && stageBase.median_seconds !== null && (
                      <div className="mt-0.5 flex justify-between text-[10px] text-ink/45">
                        <span>과거 중앙값</span>
                        <span>{formatTimingSeconds(stageBase.median_seconds)}</span>
                      </div>
                    )}
                  </div>
                ) : stageTiming?.note && s.state === "succeeded" ? (
                  <p className="mt-2 font-mono text-[10px] text-ink/40">※ {stageTiming.note}</p>
                ) : null}
              </div>

              <dl className="mt-2 space-y-0.5 text-xs border-t border-ink/10 pt-1.5">
                {metrics.length === 0 ? (
                  <div className="text-ink/40">지표 미제공</div>
                ) : (
                  metrics.map(([key, value]) => (
                    <div key={key} className="flex justify-between gap-2">
                      <dt className="font-mono text-ink/50 truncate">{key}</dt>
                      <dd className="font-mono shrink-0">{String(value)}</dd>
                    </div>
                  ))
                )}
              </dl>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
