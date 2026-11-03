import { GATE_LABEL, STAGE_LABEL } from "@/lib/labels";
import { GATE_BEFORE, type Gate, type RunStatus } from "@/lib/workflows";
import { StateBadge } from "./StateBadge";

type Props = { run: RunStatus };

function gateTone(run: RunStatus, gate: Gate): string {
  if (run.approvals.includes(gate)) return "text-emerald-700";
  if (run.waiting_gate === gate) return "text-amber-700";
  return "text-ink/40";
}

export function StageStepper({ run }: Props) {
  return (
    <ol className="grid gap-2 sm:grid-cols-5" aria-label="이전 단계">
      {run.stages.map((s, i) => {
        const gate = GATE_BEFORE[s.stage];
        const isCurrent = run.current_stage === s.stage;
        const metrics = Object.entries(s.metrics);
        return (
          <li
            key={s.stage}
            aria-current={isCurrent ? "step" : undefined}
            className={`rounded-lg border bg-white p-3 ${
              isCurrent ? "border-ink shadow-[3px_3px_0_0_var(--color-ink)]" : "border-ink/15"
            }`}
          >
            <p className={`h-4 font-mono text-[10px] tracking-wide ${gate ? gateTone(run, gate) : ""}`}>
              {gate ? `${run.approvals.includes(gate) ? "✓" : "◆"} ${GATE_LABEL[gate]}` : ""}
            </p>
            <p className="mt-1 font-mono text-[11px] text-ink/40">{String(i + 1).padStart(2, "0")}</p>
            <p className="font-medium">{STAGE_LABEL[s.stage]}</p>
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
            <dl className="mt-2 space-y-0.5 text-xs">
              {metrics.length === 0 ? (
                <div className="text-ink/40">지표 미제공</div>
              ) : (
                metrics.map(([key, value]) => (
                  <div key={key} className="flex justify-between gap-2">
                    <dt className="font-mono text-ink/50">{key}</dt>
                    <dd className="font-mono">{String(value)}</dd>
                  </div>
                ))
              )}
            </dl>
          </li>
        );
      })}
    </ol>
  );
}
