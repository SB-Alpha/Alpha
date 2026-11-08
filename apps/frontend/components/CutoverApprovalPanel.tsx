"use client";

import { useEffect, useState } from "react";
import { preflightVerdict } from "@/lib/dashboard";
import { GATE_LABEL } from "@/lib/labels";
import { BUTTON_PRIMARY, BUTTON_SECONDARY, INPUT } from "@/lib/ui";
import {
  workflows,
  type CutoverReadiness,
  type DisplayedPreflight,
  type PreflightReport,
  type RunStatus,
} from "@/lib/workflows";
import { CutoverSimulationWidget } from "./3d/CutoverSimulationWidget";
import { ReadinessTable } from "./CutoverSafety";
import { PreflightChecklist } from "./PreflightChecklist";

type Props = {
  run: RunStatus;
  runId: string;
  readiness: CutoverReadiness | null;
  isBusy: boolean;
  isTrafficUncertain: boolean;
  onApprove: (
    comment: string,
    approver: string,
    preflight: DisplayedPreflight,
    acknowledgeUnknown: boolean,
  ) => Promise<boolean | void> | boolean | void;
};

/** 어느 요청의 응답인지 key 로 남깁니다. key 가 다르면 지난 점검이므로 보여주지 않습니다. */
type PreflightResult = {
  key: string;
  report: PreflightReport | null;
  error: string | null;
};

type Warning = { text: string; tone: string };

const AMBER = "bg-amber-50 text-amber-900";
const ROSE = "bg-rose-50 text-rose-800";

/** go 판정이 확인된 경우에만 null 입니다. 점검 중이거나 실패하면 경고로 다룹니다. */
function preflightWarning(isLoading: boolean, report: PreflightReport | null): Warning | null {
  if (isLoading) return { text: "점검이 끝나지 않았습니다. 지금 승인하면 결과를 보지 않고 전환합니다.", tone: AMBER };
  if (report === null) return { text: "점검 결과가 없습니다. 지금 승인하면 점검 없이 전환합니다.", tone: AMBER };
  const verdict = preflightVerdict(report);
  if (verdict === "no_go") return { text: "실패 항목이 있습니다. 위험을 확인한 뒤 판단하세요.", tone: ROSE };
  if (verdict === "unverified") return { text: "확인하지 못한 항목이 있습니다. 직접 확인한 뒤 판단하세요.", tone: AMBER };
  return null;
}

export function CutoverApprovalPanel({ run, runId, readiness, isBusy, isTrafficUncertain, onApprove }: Props) {
  const [approver, setApprover] = useState("");
  const [comment, setComment] = useState("");
  const [isUnknownConfirmed, setIsUnknownConfirmed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<PreflightResult | null>(null);

  const requestKey = `${runId}:${attempt}`;
  const current = result?.key === requestKey ? result : null;
  const report = current?.report ?? null;
  const error = current?.error ?? null;
  const isLoading = !current;

  // 점검마다 에이전트가 두 DB를 비교하므로 2초 폴링에 묶지 않습니다. 처음과 "다시 점검" 때만 부릅니다.
  useEffect(() => {
    let isCurrent = true;
    workflows
      .getPreflight(runId)
      .then((data) => {
        if (isCurrent) setResult({ key: requestKey, report: data, error: null });
      })
      .catch((err) => {
        if (isCurrent) {
          setResult({ key: requestKey, report: null, error: err instanceof Error ? err.message : String(err) });
        }
      });

    return () => {
      isCurrent = false;
    };
  }, [runId, requestKey]);

  const warning = preflightWarning(isLoading, report);
  const hasApprover = approver.trim() !== "";
  // 지금 화면에 보이는 값을 그대로 보냅니다. 점검 중이거나 실패했으면 report 가 null 입니다.
  const displayed: DisplayedPreflight = report && { verdict: preflightVerdict(report), measured_at: report.measured_at };

  // 승인 가능 여부는 서버 판정이 정합니다. 승인 API 도 같은 규칙으로 다시 검사합니다.
  const decision = readiness?.decision ?? null;
  const isUnknownAcknowledged = decision === "needs_confirmation" && isUnknownConfirmed;
  const canApprove = hasApprover && (decision === "go" || isUnknownAcknowledged);

  // 승인은 애니메이션을 시작하지 않습니다. 아래 전환 트래픽 뷰는 실제 Worker 상태만 따라갑니다.
  const handleApproveClick = async () => {
    try {
      await onApprove(comment, approver, displayed, isUnknownAcknowledged);
    } catch {
      // 승인 실패 안내는 onApprove 쪽에서 합니다.
    }
  };

  return (
    <div id="cutover-approval-section" className="rounded-xl border-2 border-amber-400 bg-white p-5">
      <h3 className="text-lg font-semibold">{GATE_LABEL.cutover}</h3>
      <p className="mt-1 text-sm text-ink/70">
        {isTrafficUncertain
          ? "지난 전환 결과를 확인하지 못했습니다. 승인하면 전환을 다시 요청합니다. 이미 시작된 전환이면 에이전트가 거절하고 결과만 확인합니다. 전환이 끝난 것을 확인했다면 승인 대신 아래 수동 입력에 확인 내용을 메모로 남겨 기록하세요."
          : "DB 동기화가 끝났습니다. 승인하면 요청 보류 → 최종 반영 확인 → 경로 변경 → 보류 해제 순서로 전환합니다."}
      </p>

      <div className="mt-4">
        <h4 className="text-sm font-semibold">승인 조건</h4>
        <p className="text-xs text-ink/60">서버가 저장된 지표로 판정합니다. 실패 항목이 있으면 승인할 수 없습니다.</p>
        {readiness ? (
          <ReadinessTable checks={readiness.checks} />
        ) : (
          <p className="mt-2 text-sm text-ink/50">판정을 불러오는 중입니다.</p>
        )}
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
      </div>

      <div className="mt-4 border-t border-ink/10 pt-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <h4 className="text-sm font-semibold">실시간 점검 (참고)</h4>
            <p className="text-xs text-ink/60">지금 상태를 직접 재서 보여줍니다. 이 결과로는 승인을 막지 않습니다.</p>
          </div>
          <button
            type="button"
            disabled={isLoading || isBusy}
            onClick={() => setAttempt((n) => n + 1)}
            className={`${BUTTON_SECONDARY} shrink-0`}
          >
            다시 점검
          </button>
        </div>
        <PreflightChecklist report={report} isLoading={isLoading} error={error} />
      </div>

      {/* Step 3: 승인 ② 전환 트래픽 뷰 (실시간 Worker 상태 · 실측 기록 재생) */}
      <div className="mt-4 border-t border-ink/10 pt-4">
        <CutoverSimulationWidget
          run={run}
          defaultOpen={true}
          title="승인 ② 전 트래픽 게이트: 실시간 Worker 상태 · 실측 기록 재생"
        />
      </div>

      <div className="mt-4 border-t border-ink/10 pt-4">
        {warning && (
          <div role="status" className={`mb-3 rounded-md px-3 py-2 text-sm ${warning.tone}`}>
            {warning.text}
          </div>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="min-w-0">
            <label htmlFor="cutover-approver" className="text-xs font-medium text-ink/70">
              승인자 이름 (필수)
            </label>
            <input
              id="cutover-approver"
              value={approver}
              onChange={(e) => setApprover(e.target.value)}
              maxLength={100}
              required
              aria-describedby="cutover-approver-hint"
              autoComplete="name"
              className={`${INPUT} mt-1`}
            />
            <p id="cutover-approver-hint" className="mt-1 text-xs text-ink/55">
              로그인이 없어 본인이 직접 적는 값입니다. 신원은 확인하지 않습니다.
            </p>
          </div>
          <div className="min-w-0">
            <label htmlFor="cutover-comment" className="text-xs font-medium text-ink/70">
              메모 (선택)
            </label>
            <input
              id="cutover-comment"
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              className={`${INPUT} mt-1`}
            />
          </div>
        </div>
        <button
          type="button"
          disabled={isBusy || !canApprove}
          onClick={handleApproveClick}
          className={`${BUTTON_PRIMARY} mt-3 w-full sm:w-auto`}
        >
          {warning ? "경고 확인 후 전환 승인" : "전환 승인"}
        </button>
      </div>
    </div>
  );
}
