"use client";

import { useEffect, useState } from "react";
import {
  METRIC_KEYS,
  elapsedSeconds,
  formatBytes,
  formatDuration,
  isFinished,
  isSimulated,
  numberMetric,
  overallProgress,
  securityAuditSummary,
  securityTone,
  SERVING_LABEL,
  servingSide,
  stageCounts,
  tableRowTotals,
  verificationState,
  type Verification,
} from "@/lib/dashboard";
import { GATE_LABEL, STAGE_LABEL, VERIFICATION_LABEL } from "@/lib/labels";
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

const VERIFICATION_TONE: Record<Verification, Tone> = {
  running: "neutral",
  match: "good",
  mismatch: "bad",
  unverifiable: "bad",
  unknown: "bad",
};

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

function SecurityCell({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="rounded border border-ink/10 bg-white p-2">
      <span className="block text-ink/50">{label}</span>
      <span className={`mt-0.5 font-semibold ${value === null ? "text-ink/30" : TONE_CLASS[securityTone(value)]}`}>
        {value ?? MISSING}
      </span>
    </div>
  );
}

function currentStepText(run: RunStatus): string {
  if (run.state === "failed") return "실패";
  if (run.state === "aborted") return "중단";
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
  const provider = run.request.target_infra?.provider ?? "gcp";
  const ingressLabel = provider === "gcp" ? "Cloud Run 인그레스 보호" : "목적지 인그레스 보호";

  const lag = numberMetric(run, METRIC_KEYS.replicationLagBytes);
  const copyPercent = numberMetric(run, METRIC_KEYS.copyPercent);
  const rows = tableRowTotals(run);
  const mismatch = numberMetric(run, METRIC_KEYS.mismatchTables);
  const verification = verificationState(run);
  const failed = numberMetric(run, METRIC_KEYS.failedRequests);
  const cutover = numberMetric(run, METRIC_KEYS.cutoverSeconds);
  const probeRequests = numberMetric(run, METRIC_KEYS.probeRequests);
  const probeMaxMs = numberMetric(run, METRIC_KEYS.probeMaxMs);
  const failedHint =
    probeRequests === null || probeMaxMs === null
      ? undefined
      : `실측 ${probeRequests.toLocaleString("ko-KR")}건 · 최대 ${Math.round(probeMaxMs).toLocaleString("ko-KR")}ms`;
  const writeRequests = numberMetric(run, METRIC_KEYS.writeRequests);
  const writeFailed = numberMetric(run, METRIC_KEYS.writeFailedRequests);

  const rowsText = rows && `${rows.source.toLocaleString("ko-KR")} / ${rows.target.toLocaleString("ko-KR")}`;
  // 복사 중에는 목적지 행 수가 적은 것이 정상이라 빨강으로 표시하지 않습니다.
  const rowsTone: Tone = rows && rows.source === rows.target ? "good" : "neutral";

  return (
    <section aria-labelledby="dashboard-heading" className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
      <h2 id="dashboard-heading" className="sr-only">
        이전 관제 대시보드
      </h2>

      {isSimulated(run) && (
        <p role="status" className="rounded-lg border-2 border-amber-500 bg-amber-50 p-3 text-sm font-semibold text-amber-950 lg:col-span-2">
          시뮬레이션 모드입니다. 아래 CDC·전환 지표는 실제 복제나 트래픽 전환 결과가 아닙니다.
        </p>
      )}

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
        <MetricTile label="쓰기 처리" value={serving && SERVING_LABEL[serving]} />
        <MetricTile label="복제 지연" value={lag === null ? null : formatBytes(lag)} hint="CDC 원본 → 목적지" />
        <MetricTile
          label="행 수 (원본 / 목적지)"
          value={rowsText}
          tone={rowsTone}
          hint={copyPercent === null ? undefined : `복사 ${copyPercent}%`}
        />
        {verification === null ? (
          <MetricTile
            label="행 수 불일치 테이블"
            value={mismatch === null ? null : `${mismatch.toLocaleString("ko-KR")}개`}
            tone={countTone(mismatch)}
          />
        ) : (
          // 전환 전 행 수 비교 값이 앞 단계에 남아 있으므로 비교가 끝나기 전에는 개수를 보여 주지 않습니다.
          <MetricTile
            label="전환 시점 데이터 비교"
            value={VERIFICATION_LABEL[verification]}
            tone={VERIFICATION_TONE[verification]}
            hint={verification === "mismatch" && mismatch !== null ? `${mismatch.toLocaleString("ko-KR")}개 테이블` : undefined}
          />
        )}
        <MetricTile
          label="읽기 실패 (프로버)"
          value={failed === null ? null : `${failed.toLocaleString("ko-KR")}건`}
          tone={countTone(failed)}
          hint={failedHint}
        />
        <MetricTile
          label="쓰기 실패 (Worker)"
          value={writeFailed === null ? null : `${writeFailed.toLocaleString("ko-KR")}건`}
          tone={countTone(writeFailed)}
          hint={writeRequests === null ? undefined : `실측 ${writeRequests.toLocaleString("ko-KR")}건`}
        />
        <MetricTile label="전환 시간 T1−T0" value={cutover === null ? null : `${cutover.toFixed(1)}초`} />
      </dl>

      <div className="rounded-lg border border-ink/15 bg-stone-50/70 p-3 lg:col-span-2">
        <div className="flex items-center justify-between text-xs font-medium text-ink/70">
          <span>보안 & 거버넌스</span>
          {security.isCompliant && (
            <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-800">
              COMPLIANT
            </span>
          )}
        </div>
        <div className="mt-2 grid grid-cols-1 gap-2 text-xs sm:grid-cols-3">
          <SecurityCell label="목적지 DB 사설망" value={security.targetDbNetwork} />
          <SecurityCell label="비밀번호·키 암호화" value={security.secretEncryption} />
          <SecurityCell label={ingressLabel} value={security.ingressGuard} />
        </div>
      </div>
    </section>
  );
}
