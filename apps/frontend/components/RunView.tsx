"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { DynamicCanvas } from "@/components/3d/DynamicCanvas";
import { CdcJobControls } from "@/components/CdcJobControls";
import { RecoveryBanner } from "@/components/CutoverSafety";
import { CutoverTimeline } from "@/components/CutoverTimeline";
import { DashboardPanel } from "@/components/DashboardPanel";
import { FinOpsGreenOpsCard } from "@/components/FinOpsGreenOpsCard";
import { MigrationSummaryReport } from "@/components/MigrationSummaryReport";
import { PlanApproval } from "@/components/PlanApproval";
import { RunActions } from "@/components/RunActions";
import { SelfHealingCard } from "@/components/SelfHealingCard";
import { StageStepper } from "@/components/StageStepper";
import { StateBadge } from "@/components/StateBadge";
import { formatTime } from "@/lib/labels";
import { runTitle, targetSummary, workflows, type RunStatus } from "@/lib/workflows";

const POLL_MS = 2000;

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function RunView() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<RunStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isBusy, setIsBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setRun(await workflows.get(id));
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, [id]);

  useEffect(() => {
    let isCancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      await refresh();
      if (!isCancelled) timer = setTimeout(tick, POLL_MS);
    };
    void tick();
    return () => {
      isCancelled = true;
      clearTimeout(timer);
    };
  }, [refresh]);

  const act = async (command: () => Promise<RunStatus>): Promise<boolean> => {
    setIsBusy(true);
    try {
      setRun(await command());
      setError(null);
      return true;
    } catch (e) {
      setError(errorText(e));
      return false;
    } finally {
      setIsBusy(false);
    }
  };

  const handleNavigateSection = useCallback((sectionId: string) => {
    const el = document.getElementById(sectionId);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, []);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Link href="/" className="text-sm text-ink/60 hover:text-ink">
          ← 새 이전 요청
        </Link>
        {run && (
          <div className="flex flex-wrap gap-4">
            {/* db_migration 상태 API 를 같은 run_id 로 읽습니다. workflows 가 시작한 실행은 읽기 전용입니다. */}
            <Link
              href={`/db-migration/runs/${encodeURIComponent(run.run_id)}`}
              className="text-xs font-medium text-ink/70 underline underline-offset-4 hover:text-ink"
            >
              CDC 상세 →
            </Link>
            <Link
              href={`/runs/${encodeURIComponent(run.run_id)}/report`}
              className="text-xs font-medium text-ink/70 underline underline-offset-4 hover:text-ink"
            >
              시각적 요약 리포트 (/runs/{run.run_id}/report) →
            </Link>
          </div>
        )}
      </div>

      {error && (
        <p role="alert" className="rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error}
        </p>
      )}

      {run === null ? (
        !error && <p className="text-ink/50">불러오는 중…</p>
      ) : (
        <>
          <header className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <p className="font-mono text-xs text-ink/50">{run.run_id}</p>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight sm:text-3xl">
                {runTitle(run.request)}
              </h1>
              <p className="mt-1 text-sm text-ink/60">
                {run.request.aws_region} → {targetSummary(run.request)} · 시작 {formatTime(run.created_at)}
              </p>
            </div>
            <div className="text-right">
              <StateBadge state={run.state} />
              {run.message && <p className="mt-1 text-sm text-ink/70">{run.message}</p>}
            </div>
          </header>

          <RecoveryBanner run={run} />

          {/* Step 1: 상단 컴팩트 3D 토폴로지 & 베지에 곡선 CDC 아치 */}
          <DynamicCanvas run={run} onNavigateSection={handleNavigateSection} />

          <DashboardPanel run={run} />

          {/* 공시가 기준 월 비용 추정 (실측 아님) */}
          <FinOpsGreenOpsCard run={run} />

          <div id="stage-stepper-section">
            <StageStepper run={run} />
          </div>

          <PlanApproval
            run={run}
            isBusy={isBusy}
            onApprove={(comment) => act(() => workflows.approve(run.run_id, "plan", comment))}
          />

          {/* Step 3 포함: 전환 기록 및 물리 전환 시뮬레이션 / 데모 주행 */}
          <CutoverTimeline run={run} />

          <SelfHealingCard
            run={run}
            isBusy={isBusy}
            onApplySimulationInput={(output) =>
              run.current_stage && act(() => workflows.submitManual(run.run_id, run.current_stage!, output, "시뮬레이션 값"))
            }
          />

          {/* Step 3 포함: 승인 ② 대기 시 CutoverApprovalPanel 내부에 물리 전환 시뮬레이션 렌더링 */}
          <RunActions
            run={run}
            isBusy={isBusy}
            onApproveCutover={(comment, approver, preflight, acknowledgeUnknown) =>
              act(() => workflows.approve(run.run_id, "cutover", comment, approver, preflight, acknowledgeUnknown))
            }
            onManual={(output, note) =>
              run.current_stage && act(() => workflows.submitManual(run.run_id, run.current_stage!, output, note))
            }
            onResume={() => act(() => workflows.resume(run.run_id))}
            onRetryCleanup={() => act(() => workflows.retryCleanup(run.run_id))}
            onDecommission={() => act(() => workflows.decommission(run.run_id))}
            onAbort={(reason) => act(() => workflows.abort(run.run_id, reason))}
          />

          {/* Step 2: 마이그레이션 완료 후 ScrollControls 플라이스루 시각적 요약 리포트 */}
          {run.state === "succeeded" && <MigrationSummaryReport run={run} />}

          <CdcJobControls run={run} onChanged={() => void refresh()} />
        </>
      )}
    </div>
  );
}
