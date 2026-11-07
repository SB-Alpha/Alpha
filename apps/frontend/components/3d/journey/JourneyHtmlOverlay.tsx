"use client";

import { StateBadge } from "@/components/StateBadge";
import { extractStageReports } from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
  activeStageIdx: number;
  isOverview: boolean;
  onSelectStage: (stageIdx: number) => void;
}

const TONE_CLASS = {
  good: "text-emerald-700 font-semibold",
  bad: "text-rose-700 font-semibold",
  warn: "text-amber-800 font-semibold",
  neutral: "text-ink font-medium",
} as const;

export function JourneyHtmlOverlay({
  run,
  activeStageIdx,
  isOverview,
  onSelectStage,
}: Props) {
  const reports = extractStageReports(run);
  const card = reports[activeStageIdx] ?? reports[0];

  if (!card) return null;

  return (
    <aside
      aria-label="선택된 마이그레이션 단계 실측 상세"
      className="flex w-full shrink-0 flex-col justify-between border-b border-ink/15 bg-white p-3.5 text-ink md:w-72 md:border-b-0 md:border-r"
    >
      <div>
        <div className="flex items-center justify-between gap-2 border-b border-ink/15 pb-2.5">
          <div className="flex items-center gap-2">
            <span className="rounded bg-ink px-1.5 py-0.5 font-mono text-[11px] font-semibold text-paper">
              {String(card.index).padStart(2, "0")}
            </span>
            <h3 className="text-sm font-semibold">{card.label}</h3>
          </div>
          <div className="flex items-center gap-1.5">
            <StateBadge state={card.state} />
            {card.manual && (
              <span className="rounded border border-ink/20 bg-paper px-1.5 py-0.5 text-[10px] font-semibold text-ink">
                수동
              </span>
            )}
          </div>
        </div>

        {isOverview && (
          <p className="mt-2 rounded border border-indigo-200 bg-indigo-50/70 px-2.5 py-1.5 text-[11px] font-medium text-indigo-950">
            현재 <strong>전체 6단계 조망 모드</strong>입니다. 3D 모델을 클릭하거나 상단 단계 버튼을 누르면 해당 단계로 줌인합니다.
          </p>
        )}

        {card.message && (
          <p className="mt-2.5 rounded border border-ink/10 bg-paper px-2.5 py-1.5 text-xs text-ink/80">
            {card.message}
          </p>
        )}

        <dl className="mt-3 space-y-1.5 text-xs">
          {card.facts.map((f) => (
            <div
              key={f.label}
              className="flex items-baseline justify-between gap-3 border-b border-ink/5 pb-1 last:border-b-0"
            >
              <dt className="shrink-0 text-ink/60">{f.label}</dt>
              <dd className={`truncate font-mono ${TONE_CLASS[f.tone ?? "neutral"]}`}>
                {f.value}
              </dd>
            </div>
          ))}
        </dl>
      </div>

      <div className="mt-3 space-y-2 border-t border-ink/10 pt-2.5">
        {card.updatedAt && (
          <p className="font-mono text-[10px] text-ink/50">
            마지막 기록 시각: {card.updatedAt}
          </p>
        )}
        <div className="flex items-center justify-between gap-1.5">
          <button
            type="button"
            onClick={() => onSelectStage(Math.max(0, activeStageIdx - 1))}
            disabled={!isOverview && activeStageIdx === 0}
            className="flex-1 cursor-pointer rounded border border-ink/20 bg-paper px-2 py-1 font-mono text-[11px] font-medium text-ink hover:border-ink disabled:cursor-not-allowed disabled:opacity-35"
          >
            ◀ 이전 단계
          </button>
          <button
            type="button"
            onClick={() => onSelectStage(Math.min(5, activeStageIdx + 1))}
            disabled={!isOverview && activeStageIdx === 5}
            className="flex-1 cursor-pointer rounded border border-ink bg-ink px-2 py-1 font-mono text-[11px] font-medium text-paper hover:bg-ink/90 disabled:cursor-not-allowed disabled:opacity-35"
          >
            다음 단계 ▶
          </button>
        </div>
      </div>
    </aside>
  );
}
