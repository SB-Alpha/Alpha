"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { CdcJobControls } from "@/components/CdcJobControls";
import { RecoveryBanner } from "@/components/CutoverSafety";
import { CutoverTimeline } from "@/components/CutoverTimeline";
import { DashboardPanel } from "@/components/DashboardPanel";
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

  const act = async (command: () => Promise<RunStatus>) => {
    setIsBusy(true);
    try {
      setRun(await command());
      setError(null);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setIsBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <Link href="/" className="text-sm text-ink/60 hover:text-ink">
        ← 새 이전 요청
      </Link>

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

          <DashboardPanel run={run} />

          <StageStepper run={run} />

          <PlanApproval
            run={run}
            isBusy={isBusy}
            onApprove={(comment) => act(() => workflows.approve(run.run_id, "plan", comment))}
          />

          <CutoverTimeline run={run} />

          <SelfHealingCard
            run={run}
            isBusy={isBusy}
            onApplySimulationInput={(output) =>
              run.current_stage && act(() => workflows.submitManual(run.run_id, run.current_stage!, output, "시뮬레이션 값"))
            }
          />

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
            onAbort={(reason) => act(() => workflows.abort(run.run_id, reason))}
          />

          <CdcJobControls run={run} onChanged={() => void refresh()} />
        </>
      )}
    </div>
  );
}
