"use client";

import { useEffect, useState } from "react";
import { BUTTON_SECONDARY } from "@/lib/ui";
import { workflows, type RunStatus, type SelfHealingDiagnosis } from "@/lib/workflows";

type Props = {
  run: RunStatus;
  isBusy: boolean;
  onApplySimulationInput: (input: Record<string, unknown>) => void;
};

/** 어느 실패에 대한 응답인지 key 로 남깁니다. key 가 다르면 지난 실패의 결과이므로 보여주지 않습니다. */
type DiagnosisResult = {
  key: string;
  diagnosis: SelfHealingDiagnosis | null;
  error: string | null;
};

export function SelfHealingCard({ run, isBusy, onApplySimulationInput }: Props) {
  const isFailed = run.state === "failed";
  const runId = run.run_id;
  const failureKey = `${runId}:${run.updated_at}`;
  const [result, setResult] = useState<DiagnosisResult | null>(null);
  const [isRediagnosing, setIsRediagnosing] = useState(false);
  const current = result?.key === failureKey ? result : null;
  const diagnosis = current?.diagnosis ?? null;
  const error = current?.error ?? null;
  const loading = isFailed && !current;

  useEffect(() => {
    if (!isFailed) return;
    let isCurrent = true;
    workflows
      .getDiagnosis(runId)
      .then((data) => {
        if (isCurrent) setResult({ key: failureKey, diagnosis: data, error: null });
      })
      .catch((err) => {
        if (isCurrent) {
          setResult({ key: failureKey, diagnosis: null, error: err instanceof Error ? err.message : String(err) });
        }
      });

    return () => {
      isCurrent = false;
    };
  }, [runId, isFailed, failureKey]);

  if (!isFailed) return null;

  const handleRediagnose = async () => {
    setIsRediagnosing(true);
    try {
      const data = await workflows.triggerDiagnosis(runId);
      setResult({ key: failureKey, diagnosis: data, error: null });
    } catch (err) {
      // 다시 진단하지 못해도 앞서 받은 진단은 그대로 보여 줍니다.
      setResult({ key: failureKey, diagnosis, error: err instanceof Error ? err.message : String(err) });
    } finally {
      setIsRediagnosing(false);
    }
  };

  const simulationInput = diagnosis?.suggested_manual_input;
  const hasSimulationInput = !!simulationInput && Object.keys(simulationInput).length > 0;

  return (
    <section className="rounded-lg border-2 border-amber-500/30 bg-amber-50/50 p-4 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-amber-200/60 pb-3">
        <h3 className="font-semibold text-amber-950">자가치유(Self-Healing) 진단 리포트</h3>
        <div className="flex flex-wrap items-center gap-2">
          {diagnosis && (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800">
              {diagnosis.diagnosed_by === "ai" ? "Claude AI 진단" : "규칙 기반 진단"}
            </span>
          )}
          <button
            type="button"
            disabled={loading || isRediagnosing || isBusy}
            onClick={() => void handleRediagnose()}
            className={`${BUTTON_SECONDARY} !px-3 !py-1 text-xs`}
          >
            {isRediagnosing ? "재진단 중…" : "AI 재진단"}
          </button>
        </div>
      </div>

      {loading && (
        <div className="py-4 text-center text-sm text-amber-800/70">
          <span className="inline-block animate-pulse">실패 원인을 분석하는 중…</span>
        </div>
      )}

      {error && <p className="mt-3 text-xs text-rose-700">진단 조회 실패: {error}</p>}

      {diagnosis && (
        <div className="mt-3 space-y-3 text-sm">
          <div>
            <span className="font-semibold text-amber-900">근본 원인</span>
            <p className="mt-1 rounded border border-amber-200/50 bg-white/80 p-2.5 font-medium text-ink/90">
              {diagnosis.root_cause}
            </p>
          </div>

          <div>
            <span className="font-semibold text-amber-900">복구 조치</span>
            <ol className="mt-1 list-decimal space-y-1.5 pl-5 text-ink/80">
              {diagnosis.action_plan.map((step) => (
                <li key={step} className="leading-relaxed">
                  {step}
                </li>
              ))}
            </ol>
          </div>

          {hasSimulationInput && (
            <div className="mt-2 border-t border-amber-200/40 pt-2">
              <p className="mb-2 text-xs text-amber-900/80">
                시뮬레이션 전용 값입니다. 실제 인프라가 아니며 이 단계는 수동으로 표시됩니다.
              </p>
              <button
                type="button"
                disabled={isBusy}
                onClick={() => onApplySimulationInput(simulationInput)}
                className="rounded bg-amber-600 px-3 py-1.5 text-xs font-semibold text-white shadow hover:bg-amber-700 disabled:opacity-50"
              >
                시뮬레이션 값으로 수동 진행
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
