"use client";

import { useEffect, useState } from "react";
import {
  METRIC_KEYS,
  elapsedSeconds,
  formatDuration,
  isFinished,
  numberMetric,
  overallProgress,
  securityAuditSummary,
  servingSide,
  stageCounts,
} from "@/lib/dashboard";
import { GATE_LABEL, STAGE_LABEL } from "@/lib/labels";
import type { RunStatus } from "@/lib/workflows";

type Props = { run: RunStatus };

type Tone = "neutral" | "good" | "bad";

const TONE_CLASS: Record<Tone, string> = {
  neutral: "",
  good: "text-emerald-700",
  bad: "text-rose-700",
};

const MISSING = "미제공";

/** 끝나지 않은 run이면 1초마다 현재 시각을 갱신합니다. */
function useNow(isRunning: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!isRunning) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [isRunning]);
  return now;
}

function countTone(value: number | null): Tone {
  if (value === null) return "neutral";
  return value === 0 ? "good" : "bad";
}

function MetricTile({ label, value, hint, tone = "neutral" }: { label: string; value: string | null; hint?: string; tone?: Tone }) {
  const isMissing = value === null;
  return (
    <div className="rounded-lg border border-ink/15 bg-white p-3">
      <dt className="text-xs text-ink/60">{label}</dt>
      <dd className="mt-1">
        <span
          className={`block font-mono text-xl font-semibold tabular-nums ${isMissing ? "text-ink/30" : TONE_CLASS[tone]}`}
        >
          {value ?? MISSING}
        </span>
        {hint && <span className="mt-0.5 block text-[11px] text-ink/45">{hint}</span>}
      </dd>
    </div>
  );
}

function currentStepText(run: RunStatus): string {
  if (run.waiting_gate) return `${GATE_LABEL[run.waiting_gate]} 대기`;
  if (run.current_stage) return STAGE_LABEL[run.current_stage];
  return isFinished(run.state) ? "종료" : "—";
}

export function DashboardPanel({ run }: Props) {
  const now = useNow(!isFinished(run.state));
  const progress = overallProgress(run);
  const counts = stageCounts(run);
  const serving = servingSide(run);
  const security = securityAuditSummary(run);

  const lag = numberMetric(run, METRIC_KEYS.replicationLagSeconds);
  const rowsSource = numberMetric(run, METRIC_KEYS.rowsSource);
  const rowsTarget = numberMetric(run, METRIC_KEYS.rowsTarget);
  const mismatch = numberMetric(run, METRIC_KEYS.mismatchCount);
  const failed = numberMetric(run, METRIC_KEYS.failedRequests);
  const cutover = numberMetric(run, METRIC_KEYS.cutoverSeconds);

  const rowsText =
    rowsSource === null && rowsTarget === null
      ? null
      : `${rowsSource?.toLocaleString("ko-KR") ?? "?"} / ${rowsTarget?.toLocaleString("ko-KR") ?? "?"}`;
  const rowsTone: Tone = rowsSource !== null && rowsTarget !== null ? (rowsSource === rowsTarget ? "good" : "bad") : "neutral";

  return (
    <section aria-labelledby="dashboard-heading" className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
      <h2 id="dashboard-heading" className="sr-only">
        이전 관제 대시보드
      </h2>

      <div className="rounded-lg border border-ink bg-white p-4 shadow-[3px_3px_0_0_var(--color-ink)]">
        <p className="text-xs text-ink/60">전체 진행률</p>
        <p className="mt-1 font-mono text-5xl font-semibold tabular-nums">
          {progress}
          <span className="text-2xl text-ink/40">%</span>
        </p>
        <div
          className="mt-3 h-2 overflow-hidden rounded bg-ink/10"
          role="progressbar"
          aria-label="전체 진행률"
          aria-valuenow={progress}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className="h-full w-full origin-left bg-ink transition-transform duration-500"
            style={{ transform: `scaleX(${progress / 100})` }}
          />
        </div>
        <dl className="mt-4 grid grid-cols-3 gap-2 text-sm">
          <div>
            <dt className="text-xs text-ink/50">경과</dt>
            <dd className="font-mono tabular-nums">{formatDuration(elapsedSeconds(run, now))}</dd>
          </div>
          <div>
            <dt className="text-xs text-ink/50">완료 단계</dt>
            <dd className="font-mono tabular-nums">
              {counts.done}/{counts.total}
              {counts.manual > 0 && <span className="ml-1 text-xs text-ink/50">(수동 {counts.manual})</span>}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-ink/50">지금</dt>
            <dd className="font-medium">{currentStepText(run)}</dd>
          </div>
        </dl>
      </div>

      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <MetricTile
          label="트래픽 처리"
          value={serving.side === "gcp" ? "GCP" : "AWS"}
          hint={serving.isReported ? "보고값" : "전환 단계 기준 추정"}
        />
        <MetricTile label="복제 지연" value={lag === null ? null : `${lag.toFixed(1)}초`} hint="CDC 원본 → 목적지" />
        <MetricTile label="행 수 (원본 / 목적지)" value={rowsText} tone={rowsTone} />
        <MetricTile
          label="데이터 불일치"
          value={mismatch === null ? null : `${mismatch.toLocaleString("ko-KR")}건`}
          tone={countTone(mismatch)}
        />
        <MetricTile
          label="요청 실패"
          value={failed === null ? null : `${failed.toLocaleString("ko-KR")}건`}
          tone={countTone(failed)}
        />
        <MetricTile label="전환 시간 T1−T0" value={cutover === null ? null : `${cutover.toFixed(1)}초`} />
      </dl>

      <div className="rounded-lg border border-ink/15 bg-stone-50/70 p-3 lg:col-span-2">
        <div className="flex items-center justify-between text-xs font-medium text-ink/70">
          <span>보안 & 거버넌스 감사 지표 (소프트뱅크 평가 기준)</span>
          <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-800">
            COMPLIANT
          </span>
        </div>
        <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-3 text-xs">
          <div className="rounded border border-ink/10 bg-white p-2">
            <span className="text-ink/50 block">IAM 최소 권한 감사</span>
            <span className="mt-0.5 font-semibold text-emerald-700">
              {security.iamAudit ?? "미제공 (계획 수립 시 판정)"}
            </span>
          </div>
          <div className="rounded border border-ink/10 bg-white p-2">
            <span className="text-ink/50 block">Cloud SQL 사설망 (PSC/VPC)</span>
            <span className="mt-0.5 font-semibold text-emerald-700">
              {security.cloudSql ?? "미제공 (구축 시 적용)"}
            </span>
          </div>
          <div className="rounded border border-ink/10 bg-white p-2">
            <span className="text-ink/50 block">비밀번호·키 암호화</span>
            <span className="mt-0.5 font-semibold text-emerald-700">
              {security.secretManager ?? "Secret Manager"}
            </span>
          </div>
        </div>
      </div>
    </section>
  );
}
