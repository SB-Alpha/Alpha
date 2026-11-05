"use client";

import { useEffect, useState } from "react";
import { workflows, type RunStatus, type SelfHealingDiagnosis } from "@/lib/workflows";

type Props = {
  run: RunStatus;
  onApplyManual?: (input: Record<string, unknown>) => void;
};

export function SelfHealingCard({ run, onApplyManual }: Props) {
  const isFailed = run.state === "failed" || run.state === "paused";
  const [diagnosis, setDiagnosis] = useState<SelfHealingDiagnosis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loading = isFailed && !diagnosis && !error;

  useEffect(() => {
    if (!isFailed) return;
    let isCurrent = true;
    workflows
      .getDiagnosis(run.run_id)
      .then((data) => {
        if (isCurrent) setDiagnosis(data);
      })
      .catch((err) => {
        if (isCurrent) setError(err instanceof Error ? err.message : String(err));
      });

    return () => {
      isCurrent = false;
    };
  }, [run.run_id, isFailed, run.updated_at]);

  if (!isFailed) return null;

  return (
    <section className="rounded-lg border-2 border-amber-500/30 bg-amber-50/50 p-4 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-amber-200/60 pb-3">
        <div className="flex items-center gap-2">
          <span className="text-xl">🤖</span>
          <h3 className="font-semibold text-amber-950">
            Agentic AI 자가치유(Self-Healing) 진단 리포트
          </h3>
        </div>
        {diagnosis && (
          <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800">
            {diagnosis.diagnosed_by === "ai" ? "✨ Claude Opus 5.5 AI 진단" : "⚙️ 규칙 기반 진단"}
          </span>
        )}
      </div>

      {loading && (
        <div className="py-4 text-center text-sm text-amber-800/70">
          <span className="inline-block animate-pulse">장애 로그를 분석하고 자가치유 진단 리포트를 생성하는 중…</span>
        </div>
      )}

      {error && (
        <p className="mt-3 text-xs text-rose-700">진단 조회 실패: {error}</p>
      )}

      {diagnosis && (
        <div className="mt-3 space-y-3 text-sm">
          <div>
            <span className="font-semibold text-amber-900">근본 원인 (Root Cause):</span>
            <p className="mt-1 rounded bg-white/80 p-2.5 font-medium text-ink/90 border border-amber-200/50">
              {diagnosis.root_cause}
            </p>
          </div>

          <div>
            <span className="font-semibold text-amber-900">추천 복구 조치 (Remediation Plan):</span>
            <ol className="mt-1 list-decimal space-y-1.5 pl-5 text-ink/80">
              {diagnosis.action_plan.map((step, idx) => (
                <li key={idx} className="leading-relaxed">
                  {step}
                </li>
              ))}
            </ol>
          </div>

          {diagnosis.suggested_manual_input && onApplyManual && (
            <div className="mt-2 pt-2 border-t border-amber-200/40">
              <button
                type="button"
                onClick={() => onApplyManual(diagnosis.suggested_manual_input!)}
                className="inline-flex items-center gap-1.5 rounded bg-amber-600 px-3 py-1.5 text-xs font-semibold text-white shadow hover:bg-amber-700"
              >
                <span>⚡</span>
                <span>AI 권장 픽스처로 수동 주입 실행</span>
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
