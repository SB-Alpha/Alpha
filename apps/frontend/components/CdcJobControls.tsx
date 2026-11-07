"use client";

import { useState } from "react";
import { isTrafficOnSource } from "@/lib/cutover";
import { canAbort, canClose, canRetry, dbMigration, type CdcJobSummary } from "@/lib/dbMigration";
import { BUTTON_DANGER, BUTTON_SECONDARY } from "@/lib/ui";
import { ApiError, type RunStatus } from "@/lib/workflows";

type Props = {
  run: RunStatus;
  onChanged: () => void;
};

type Action = {
  key: "retry" | "close" | "abort";
  label: string;
  confirm: string;
  buttonClass: string;
  isAllowed: (job: CdcJobSummary, run: RunStatus) => boolean;
};

const ACTIONS: Action[] = [
  {
    key: "retry",
    label: "실패한 복제 단계 재시도",
    confirm: "실패한 커넥터를 다시 시작합니다. 계속할까요?",
    buttonClass: BUTTON_SECONDARY,
    isAllowed: canRetry,
  },
  {
    key: "close",
    label: "CDC 복제 강제 종료",
    confirm:
      "커넥터와 복제 슬롯을 삭제해 복제를 끝냅니다. 이후에는 원본으로 되돌릴 복제 경로가 없습니다. 안정화 확인을 마쳤나요?",
    buttonClass: BUTTON_DANGER,
    isAllowed: canClose,
  },
  {
    key: "abort",
    label: "복제 작업 강제 중단",
    confirm:
      "이 복제 작업만 멈추고 커넥터와 슬롯을 지웁니다. VPN 은 남고 run 은 중단되지 않습니다. 전체 정리는 '이전 중단'을 쓰세요. 계속할까요?",
    buttonClass: BUTTON_DANGER,
    isAllowed: (job, run) => canAbort(job) && isTrafficOnSource(run),
  },
];

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 404) return "에이전트에 이 작업이 없습니다. 복제가 아직 시작되지 않았을 수 있습니다.";
    if (err.status === 0 || err.status >= 500) return "CDC 에이전트에서 응답을 받지 못했습니다. 작업이 진행 중일 수 있으니 상태를 확인하세요.";
  }
  return "CDC 조작 중 오류가 발생했습니다. 잠시 후 다시 시도하거나 서버 로그를 확인하세요.";
}

/** run 상태 머신이 처리하지 못하는 경우를 위한 비상 조작입니다. 평소에는 접어 둡니다. */
export function CdcJobControls({ run, onChanged }: Props) {
  const [job, setJob] = useState<CdcJobSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isWorking, setIsWorking] = useState(false);

  const jobId = run.cdc_job_id;
  const stage = run.stages.find((s) => s.stage === "db_migration");
  if (!jobId || !stage || stage.state === "pending" || stage.manual) return null;

  const load = async () => {
    setIsWorking(true);
    try {
      setJob(await dbMigration.getJob(jobId));
      setError(null);
    } catch (err) {
      setJob(null);
      setError(errorText(err));
    } finally {
      setIsWorking(false);
    }
  };

  const perform = async (action: Action) => {
    if (!window.confirm(action.confirm)) return;
    setIsWorking(true);
    try {
      setJob(await dbMigration[action.key](jobId));
      setError(null);
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setIsWorking(false);
    }
  };

  return (
    <details
      className="rounded-xl border border-rose-200 bg-white p-5"
      onToggle={(e) => {
        if (e.currentTarget.open) void load();
      }}
    >
      <summary className="cursor-pointer text-sm font-semibold text-rose-900">긴급 조작: CDC 복제 작업</summary>
      <p className="mt-2 text-xs text-ink/60">
        run 이 멈췄을 때만 씁니다. 평소에는 위의 승인과 이전 중단을 쓰세요. 작업 ID <span className="font-mono">{jobId}</span>
      </p>
      <p className="mt-3 text-sm" role="status">
        {job ? (
          <>
            상태 <span className="font-mono">{job.state}</span> · 단계 <span className="font-mono">{job.phase ?? "미제공"}</span>
          </>
        ) : isWorking ? (
          "작업 상태를 확인하는 중…"
        ) : (
          "작업 상태 미제공"
        )}
      </p>
      {error && (
        <p role="alert" className="mt-2 rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error}
        </p>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" disabled={isWorking} onClick={() => void load()} className={BUTTON_SECONDARY}>
          상태 새로고침
        </button>
        {ACTIONS.map((action) => (
          <button
            key={action.key}
            type="button"
            disabled={isWorking || !job || !action.isAllowed(job, run)}
            onClick={() => void perform(action)}
            className={action.buttonClass}
          >
            {action.label}
          </button>
        ))}
      </div>
      {job && canAbort(job) && !isTrafficOnSource(run) && (
        <p className="mt-2 text-xs text-ink/60">
          트래픽이 원본에 있다고 확인되지 않아 강제 중단을 막았습니다. Worker 상태와 데이터를 확인한 뒤 처리하세요.
        </p>
      )}
    </details>
  );
}
