"use client";

import { Scroll } from "@react-three/drei";
import { StateBadge } from "@/components/StateBadge";
import { extractStageReports } from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
}

const TONE_CLASS = {
  good: "text-emerald-700 font-semibold",
  bad: "text-rose-700 font-semibold",
  warn: "text-amber-800 font-semibold",
  neutral: "text-ink font-medium",
} as const;

export function JourneyHtmlOverlay({ run }: Props) {
  const reports = extractStageReports(run);

  return (
    <Scroll html style={{ width: "100%", pointerEvents: "none" }}>
      {reports.map((card, i) => (
        <div
          key={card.stage}
          style={{ top: `${i * 100 + 6}vh` }}
          className="pointer-events-auto absolute left-4 max-w-md select-none rounded-lg border border-ink bg-white p-4 text-ink shadow-[3px_3px_0_0_var(--color-ink)] sm:left-8"
        >
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink/15 pb-2">
            <div className="flex items-center gap-2">
              <span className="font-mono text-xs text-ink/45">
                {String(card.index).padStart(2, "0")}
              </span>
              <h3 className="text-sm font-semibold">{card.label}</h3>
            </div>
            <div className="flex items-center gap-1.5">
              <StateBadge state={card.state} />
              {card.manual && (
                <span className="rounded bg-ink px-1.5 py-0.5 text-[10px] font-semibold text-paper">
                  수동
                </span>
              )}
            </div>
          </div>

          {card.message && (
            <p className="mt-2 rounded border border-ink/10 bg-paper px-2.5 py-1.5 text-xs text-ink/80">
              {card.message}
            </p>
          )}

          <dl className="mt-2.5 space-y-1 text-xs">
            {card.facts.map((f) => (
              <div key={f.label} className="flex items-baseline justify-between gap-3">
                <dt className="shrink-0 text-ink/55">{f.label}</dt>
                <dd className={`truncate font-mono ${TONE_CLASS[f.tone ?? "neutral"]}`}>
                  {f.value}
                </dd>
              </div>
            ))}
          </dl>

          {card.updatedAt && (
            <p className="mt-2 border-t border-ink/10 pt-1.5 font-mono text-[10px] text-ink/45">
              마지막 기록 시각: {card.updatedAt}
            </p>
          )}
        </div>
      ))}
    </Scroll>
  );
}
