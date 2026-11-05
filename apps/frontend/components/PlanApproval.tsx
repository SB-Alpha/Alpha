"use client";

import { useState } from "react";
import { canApprovePlan, readPlan, type CheckResult } from "@/lib/plan";
import { BUTTON_PRIMARY, INPUT } from "@/lib/ui";
import type { RunStatus } from "@/lib/workflows";

type Props = {
  run: RunStatus;
  isBusy: boolean;
  onApprove: (comment: string) => void;
};

const RESULT_LABEL: Record<CheckResult, string> = { pass: "통과", fail: "실패", unknown: "확인 불가" };
const RESULT_TONE: Record<CheckResult, string> = {
  pass: "text-emerald-700",
  fail: "text-rose-700",
  unknown: "text-amber-700",
};
const RISK_TONE = { high: "border-rose-400", medium: "border-amber-400", low: "border-ink/20" } as const;

export function PlanApproval({ run, isBusy, onApprove }: Props) {
  const [comment, setComment] = useState("");
  const plan = readPlan(run.outputs.planning);
  const decision = canApprovePlan(plan);
  const isWaiting = run.state === "waiting_approval" && run.waiting_gate === "plan";
  const isApproved = run.approvals.includes("plan");

  return (
    <section aria-labelledby="plan-heading" className="rounded-xl border border-ink/15 bg-white p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="plan-heading" className="text-lg font-semibold">
          승인 ① 이전 계획
        </h2>
        {isApproved && <span className="text-sm font-medium text-emerald-700">승인 완료</span>}
        {!isApproved && !isWaiting && <span className="text-sm text-ink/50">계획이 나오면 여기서 승인합니다</span>}
      </div>

      {plan === null ? (
        <p className="mt-3 text-sm text-ink/50">planning 결과 미제공</p>
      ) : (
        <div className="mt-4 grid gap-6 md:grid-cols-[3fr_2fr]">
          <div>
            <h3 className="text-xs font-semibold tracking-wide text-ink/50">
              요약{plan.summarySource === "ai" ? " · AI 작성" : plan.summarySource === "rule" ? " · 규칙 기반" : ""}
            </h3>
            {plan.summary.length === 0 ? (
              <p className="mt-2 text-sm text-ink/50">미제공</p>
            ) : (
              <ol className="mt-2 space-y-1.5 text-[15px] leading-relaxed">
                {plan.summary.map((line, i) => (
                  <li key={`${i}-${line}`} className="flex gap-3">
                    <span className="font-mono text-ink/35">{i + 1}</span>
                    <span>{line}</span>
                  </li>
                ))}
              </ol>
            )}
            {plan.risks.length > 0 && (
              <>
                <h3 className="mt-5 text-xs font-semibold tracking-wide text-ink/50">위험</h3>
                <ul className="mt-2 space-y-2">
                  {plan.risks.map((r, i) => (
                    <li key={`${r.title}-${i}`} className={`border-l-2 pl-3 text-sm ${RISK_TONE[r.level]}`}>
                      <p className="font-medium">{r.title}</p>
                      {r.detail && <p className="text-ink/60">{r.detail}</p>}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
          <div>
            <h3 className="text-xs font-semibold tracking-wide text-ink/50">필수 조건 (코드 검사)</h3>
            {plan.checks.length === 0 ? (
              <p className="mt-2 text-sm text-ink/50">미제공</p>
            ) : (
              <ul className="mt-1 divide-y divide-ink/10">
                {plan.checks.map((c) => (
                  <li key={c.id} className="py-2">
                    <div className="flex justify-between gap-2 text-sm">
                      <span>{c.title}</span>
                      <span className={`shrink-0 font-medium ${RESULT_TONE[c.result]}`}>{RESULT_LABEL[c.result]}</span>
                    </div>
                    {c.detail && <p className="text-xs text-ink/55">{c.detail}</p>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      {isWaiting && (
        <form
          className="mt-5 border-t border-ink/10 pt-4"
          onSubmit={(e) => {
            e.preventDefault();
            onApprove(comment);
          }}
        >
          {decision.reason && (
            <div
              role="status"
              className={`mb-3 rounded-md px-3 py-2 text-sm ${
                decision.allowed ? "bg-amber-50 text-amber-900" : "bg-rose-50 text-rose-800"
              }`}
            >
              {decision.reason}
              {plan && plan.blocking.length > 0 && (
                <ul className="mt-1 list-disc pl-5">
                  {plan.blocking.map((b, i) => (
                    <li key={`${b}-${i}`}>{b}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
          <div className="flex flex-col gap-2 sm:flex-row">
            <label className="sr-only" htmlFor="plan-comment">
              승인 메모
            </label>
            <input
              id="plan-comment"
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="메모 (선택)"
              className={INPUT}
            />
            <button type="submit" disabled={!decision.allowed || isBusy} className={`${BUTTON_PRIMARY} shrink-0`}>
              {plan?.approvable === false ? "위험 감수하고 계획 승인" : "계획 승인"}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
