"use client";

import {
  SERVING_LABEL,
  countTone,
  type CutoverOverlayFacts,
  type CutoverSecondsSource,
  type ServingSide,
} from "@/lib/dashboard";
import type { RequestRate, WorkerLive } from "@/lib/edgeLive";
import type { CutoverPhase } from "./CutoverPhysicsScene";

// 전환 트래픽 뷰의 정보 영역. 3D 장면을 가리지 않도록 캔버스 위(상태 바)와 아래(정보 패널)에 둡니다.

/** 지금 그리는 게이트 상태와 그 출처. live 는 실시간 Worker, replay 는 실측 단계 기록 재생입니다. */
export interface PhaseView {
  phase: CutoverPhase;
  source: "live" | "replay" | "replay_done";
  stepLabel: string | null;
}

export interface ReplayControls {
  canReplay: boolean;
  note: string | null;
  isActive: boolean;
  onStart: () => void;
  onExit: () => void;
}

export interface TrafficView {
  worker: WorkerLive | null;
  fetchError: string | null;
  rate: RequestRate | null;
  /** 실측 비율이 화면 한도를 넘을 때 구슬 1개가 뜻하는 요청 수. 넘지 않으면 null 입니다. */
  requestsPerBall: number | null;
  isReplay: boolean;
}

const MISSING = "미제공";

const PHASE_LABEL: Record<CutoverPhase, string> = {
  source: "원본 처리 (SOURCE)",
  hold: "요청 보류 (HOLD)",
  switch: "보류 중 · 목적지로 경로 변경됨",
  target: "목적지 처리 (TARGET)",
  failed: "전환 실패 기록",
  unknown: `게이트 상태 ${MISSING}`,
};

const PHASE_TONE: Record<CutoverPhase, string> = {
  source: "bg-indigo-100 text-indigo-900 border-indigo-300",
  hold: "bg-amber-100 text-amber-900 border-amber-300",
  switch: "bg-purple-100 text-purple-900 border-purple-300",
  target: "bg-emerald-100 text-emerald-900 border-emerald-300",
  failed: "bg-rose-100 text-rose-900 border-rose-300",
  unknown: "bg-stone-100 text-stone-700 border-stone-300",
};

const SECONDS_SOURCE_LABEL: Record<CutoverSecondsSource, string> = {
  worker: "Worker 측정",
  agent: "에이전트 보고",
};

const COUNT_TONE_CLASS = {
  good: "text-emerald-700",
  bad: "text-rose-700",
  neutral: "",
} as const;

const PHASE_SOURCE_LABEL: Record<PhaseView["source"], string> = {
  live: "실시간 Worker 상태",
  replay: "실측 기록 재생 중 (1배속)",
  replay_done: "실측 기록 재생 종료",
};

function FailureCount({ value }: { value: number | null }) {
  return (
    <span className={`font-semibold ${COUNT_TONE_CLASS[countTone(value)]}`}>
      {value !== null ? `${value}건` : MISSING}
    </span>
  );
}

function stepsText(steps: CutoverOverlayFacts["steps"]): string {
  if (!steps || steps.length === 0) return MISSING;
  return steps
    .map((s) => `${s.step} ${s.elapsedMs !== null ? `${s.elapsedMs}ms` : MISSING}`)
    .join(" · ");
}

function workerStatusText(traffic: TrafficView): string | null {
  if (traffic.fetchError) return `실시간 조회 실패: ${traffic.fetchError}`;
  const worker = traffic.worker;
  if (!worker) return "실시간 상태 불러오는 중…";
  if (worker.status === "not_configured") return "Worker 미설정 (WORKER_URL 없음)";
  if (worker.status === "error") return `Worker 조회 실패: ${worker.error ?? MISSING}`;
  return null;
}

function trafficNote(traffic: TrafficView, phase: CutoverPhase): string {
  if (traffic.isReplay) return "재생 중에는 그때의 트래픽 기록이 없어 구슬을 그리지 않습니다.";
  if (phase === "unknown") return "게이트 상태를 몰라 경로를 그릴 수 없습니다. 트래픽 미제공";
  if (!traffic.rate || (traffic.rate.writePerSec === null && traffic.rate.readPerSec === null)) {
    return "트래픽 미제공 (요청 비율은 두 번째 관측부터 계산합니다)";
  }
  const base =
    "구슬은 Worker 누적 요청 수(쓰기+읽기)의 초당 증가량만큼만 떨어집니다. ALPHA_PROBE_URL 이 Worker 를 거치면 이 화면의 실시간 프로브 요청(백엔드 1초 캐시라 최대 1건/초)도 읽기에 들어갑니다.";
  return traffic.requestsPerBall !== null
    ? `${base} 구슬 1개 ≈ 요청 ${traffic.requestsPerBall.toFixed(1)}건`
    : base;
}

const countText = (value: number | null | undefined) =>
  typeof value === "number" ? value.toLocaleString("ko-KR") : MISSING;
const rateText = (value: number | null | undefined) =>
  typeof value === "number" ? `${value.toFixed(1)}/s` : MISSING;

/** 캔버스 위: 게이트 상태(출처·단계)와 실측 기록 재생 버튼. */
export function CutoverTopBar({
  phaseView,
  traffic,
  replay,
}: {
  phaseView: PhaseView;
  traffic: TrafficView;
  replay: ReplayControls;
}) {
  const statusText = phaseView.source === "live" ? workerStatusText(traffic) : null;
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink/15 bg-white px-3 py-2 font-mono text-[11px]">
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <span className="flex items-center gap-1.5 font-semibold text-ink">
          <span
            className={`inline-block h-2 w-2 rounded-full ${phaseView.source === "live" ? "bg-emerald-600" : "bg-ink/40"}`}
          />
          {PHASE_SOURCE_LABEL[phaseView.source]}
        </span>
        <span className={`rounded border px-1.5 py-0.5 font-bold ${PHASE_TONE[phaseView.phase]}`}>
          {PHASE_LABEL[phaseView.phase]}
        </span>
        {phaseView.stepLabel && (
          <span className="rounded border border-ink/25 bg-paper px-1.5 py-0.5 text-[10px] font-medium text-ink/70">
            {phaseView.stepLabel}
          </span>
        )}
        {statusText && <span className="text-[10px] text-ink/60">{statusText}</span>}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {replay.note && <span className="text-[10px] text-ink/60">{replay.note}</span>}
        {replay.isActive ? (
          <button
            type="button"
            onClick={replay.onExit}
            className="rounded border border-ink bg-white px-2.5 py-1 text-xs font-semibold text-ink transition-transform hover:-translate-y-px"
          >
            실시간 보기로 돌아가기
          </button>
        ) : (
          <button
            type="button"
            disabled={!replay.canReplay}
            onClick={replay.onStart}
            title="이 run 의 cutover_steps 경과 시간(elapsed_ms)을 실제 시간 그대로 다시 보여 줍니다"
            className="rounded bg-ink px-2.5 py-1 text-xs font-semibold text-paper transition-transform hover:-translate-y-px disabled:opacity-50"
          >
            ▶ 실측 기록 재생
          </button>
        )}
      </div>
    </div>
  );
}

// Worker 가 보고한 누적 요청 수입니다. 구슬 수를 세지 않습니다.
function WorkerCounters({ traffic }: { traffic: TrafficView }) {
  const worker = traffic.worker?.status === "ok" ? traffic.worker : null;
  const counts = worker?.request_counts ?? null;
  return (
    <div className="grid grid-cols-3 gap-2">
      <div className="min-w-0 rounded-md border border-ink/20 bg-white px-3 py-2">
        <span className="block text-[10px] text-ink/55">쓰기 (Worker 누적)</span>
        <span className="block truncate font-mono text-base font-bold tabular-nums">{countText(counts?.write_total)}</span>
        <span className="block font-mono text-[10px] text-ink/55">
          실패 {countText(counts?.write_failed)} · {rateText(traffic.rate?.writePerSec)}
        </span>
      </div>
      <div className="min-w-0 rounded-md border border-amber-400 bg-amber-50 px-3 py-2 text-center">
        <span className="block text-[10px] font-medium text-amber-900">보류 중 요청 (Worker)</span>
        <span className="block font-mono text-base font-bold tabular-nums text-amber-900">{countText(worker?.held_count)}</span>
        <span className="block font-mono text-[10px] text-amber-900/70">원본 처리 중 {countText(worker?.inflight_source)}</span>
      </div>
      <div className="min-w-0 rounded-md border border-ink/20 bg-white px-3 py-2 text-right">
        <span className="block text-[10px] text-ink/55">읽기 (Worker 누적)</span>
        <span className="block truncate font-mono text-base font-bold tabular-nums">{countText(counts?.read_total)}</span>
        <span className="block font-mono text-[10px] text-ink/55">
          실패 {countText(counts?.read_failed)} · {rateText(traffic.rate?.readPerSec)}
        </span>
      </div>
    </div>
  );
}

// 이 run 이 실제로 보고한 값만 보여 줍니다.
function MeasuredFacts({ measured, reportedServing }: { measured: CutoverOverlayFacts; reportedServing: ServingSide | null }) {
  const { cutoverSeconds, cutoverSecondsSource } = measured;
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 rounded-md border border-ink/20 bg-white px-3 py-2 font-mono text-[10px] text-ink/75">
      <dt className="text-ink/55">처리 측</dt>
      <dd>{reportedServing ? SERVING_LABEL[reportedServing] : MISSING}</dd>
      <dt className="text-ink/55">전환 소요</dt>
      <dd>
        {cutoverSeconds !== null ? `${cutoverSeconds.toFixed(1)}초` : MISSING} (
        {cutoverSecondsSource ? SECONDS_SOURCE_LABEL[cutoverSecondsSource] : "출처 미제공"})
      </dd>
      <dt className="text-ink/55">실패 요청</dt>
      <dd>
        읽기 <FailureCount value={measured.failedRequests} /> (프로버) · 쓰기{" "}
        <FailureCount value={measured.writeFailedRequests} /> (Worker)
      </dd>
      <dt className="text-ink/55">경과</dt>
      <dd className="break-words">{stepsText(measured.steps)}</dd>
    </dl>
  );
}

/** 캔버스 아래: Worker 누적 요청 수, 이 run 의 보고값, 트래픽 설명. */
export function CutoverInfoPanel({
  phaseView,
  traffic,
  measured,
  reportedServing,
}: {
  phaseView: PhaseView;
  traffic: TrafficView;
  measured: CutoverOverlayFacts;
  reportedServing: ServingSide | null;
}) {
  return (
    <div className="grid gap-2 border-t border-ink/15 bg-paper px-3 py-2.5 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <div className="space-y-2">
        <WorkerCounters traffic={traffic} />
        <p className="font-mono text-[10px] leading-relaxed text-ink/60">{trafficNote(traffic, phaseView.phase)}</p>
      </div>
      <div className="space-y-1">
        <p className="text-[10px] font-semibold text-ink/55">이 run 보고값</p>
        <MeasuredFacts measured={measured} reportedServing={reportedServing} />
      </div>
    </div>
  );
}
