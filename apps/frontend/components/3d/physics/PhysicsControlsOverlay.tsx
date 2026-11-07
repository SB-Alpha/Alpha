"use client";

import {
  SERVING_LABEL,
  countTone,
  type CutoverOverlayFacts,
  type CutoverSecondsSource,
  type ServingSide,
} from "@/lib/dashboard";
import type { RequestRate, WorkerLive } from "@/lib/edgeLive";
import type { StageReportCardData } from "@/lib/topology3d";
import type { CutoverPhase } from "./CutoverPhysicsScene";
import type { CameraMode, DrivingControlState } from "./PipelineDrivingScene";

export type Step3Mode = "cutover_valve" | "driving_demo";

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

interface Props {
  mode: Step3Mode;
  onChangeMode: (mode: Step3Mode) => void;
  // 이 run 의 보고값 (실시간 게이트 상태와 별개)
  measured: CutoverOverlayFacts;
  reportedServing: ServingSide | null;
  // Cutover Valve Mode props
  phaseView: PhaseView;
  replay: ReplayControls;
  traffic: TrafficView;
  // Driving Demo Mode props
  drivingCheckpoint: StageReportCardData | null;
  speedKmh: number;
  gear?: "P" | "D" | "R";
  cameraMode?: CameraMode;
  onChangeCameraMode?: (mode: CameraMode) => void;
  onUpdateDriveControl: (patch: Partial<DrivingControlState>) => void;
  onResetVehicle: () => void;
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

// 이 run 이 실제로 보고한 값만 보여 줍니다.
function MeasuredFacts({ measured, reportedServing }: { measured: CutoverOverlayFacts; reportedServing: ServingSide | null }) {
  const { cutoverSeconds, cutoverSecondsSource } = measured;
  return (
    <div className="pointer-events-auto rounded-md border border-ink/20 bg-white/95 px-3 py-1 font-mono text-[10px] text-ink/75 shadow-xs">
      <p className="flex flex-wrap items-center gap-x-1">
        <span>
          이 run 보고값: 처리 측 {reportedServing ? SERVING_LABEL[reportedServing] : MISSING} · 전환 소요{" "}
          {cutoverSeconds !== null ? `${cutoverSeconds.toFixed(1)}초` : MISSING} (
          {cutoverSecondsSource ? SECONDS_SOURCE_LABEL[cutoverSecondsSource] : "출처 미제공"}) · 읽기 실패{" "}
          <FailureCount value={measured.failedRequests} /> (프로버) · 쓰기 실패{" "}
          <FailureCount value={measured.writeFailedRequests} /> (Worker)
        </span>
      </p>
      <p>전환 시작 후 경과: {stepsText(measured.steps)}</p>
    </div>
  );
}

const PHASE_SOURCE_LABEL: Record<PhaseView["source"], string> = {
  live: "실시간 Worker 상태",
  replay: "실측 기록 재생 중 (1배속)",
  replay_done: "실측 기록 재생 종료",
};

function workerStatusText(traffic: TrafficView): string | null {
  if (traffic.fetchError) return `실시간 조회 실패: ${traffic.fetchError}`;
  const worker = traffic.worker;
  if (!worker) return "실시간 상태 불러오는 중…";
  if (worker.status === "not_configured") return "Worker 미설정 (WORKER_URL 없음)";
  if (worker.status === "error") return `Worker 조회 실패: ${worker.error ?? MISSING}`;
  return null;
}

function PhaseBar({ phaseView, traffic }: { phaseView: PhaseView; traffic: TrafficView }) {
  const statusText = phaseView.source === "live" ? workerStatusText(traffic) : null;
  return (
    <div className="pointer-events-auto flex flex-wrap items-center justify-between gap-1 rounded-md border border-ink/20 bg-white/95 px-3 py-1.5 text-[11px] font-mono shadow-xs">
      <span className="flex items-center gap-1.5 font-semibold text-ink">
        <span className={`inline-block h-2 w-2 rounded-full ${phaseView.source === "live" ? "bg-emerald-600" : "bg-ink/40"}`} />
        {PHASE_SOURCE_LABEL[phaseView.source]}
        {phaseView.stepLabel && (
          <span className="rounded border border-ink/25 bg-paper px-1.5 py-0.5 text-[10px] font-medium text-ink/70">
            {phaseView.stepLabel}
          </span>
        )}
      </span>
      <span className="flex flex-wrap items-center gap-1.5">
        {statusText && <span className="text-[10px] text-ink/60">{statusText}</span>}
        <span className={`rounded border px-1.5 py-0.5 font-bold ${PHASE_TONE[phaseView.phase]}`}>
          {PHASE_LABEL[phaseView.phase]}
        </span>
      </span>
    </div>
  );
}

const countText = (value: number | null | undefined) =>
  typeof value === "number" ? value.toLocaleString("ko-KR") : MISSING;
const rateText = (value: number | null | undefined) =>
  typeof value === "number" ? `${value.toFixed(1)}/s` : MISSING;

// Worker 가 보고한 누적 요청 수입니다. 구슬 수를 세지 않습니다.
function WorkerCounters({ traffic }: { traffic: TrafficView }) {
  const worker = traffic.worker?.status === "ok" ? traffic.worker : null;
  const counts = worker?.request_counts ?? null;
  return (
    <div className="grid grid-cols-3 gap-2">
      <div className="pointer-events-auto rounded-md border border-ink/20 bg-white/95 px-3 py-1.5 shadow-xs">
        <span className="block text-[10px] text-ink/55">쓰기 (실시간 · Worker 누적)</span>
        <span className="font-mono text-sm font-bold tabular-nums">
          {countText(counts?.write_total)}
          <span className="ml-1 text-[10px] font-normal text-ink/55">실패 {countText(counts?.write_failed)}</span>
        </span>
        <span className="block font-mono text-[10px] text-ink/55">비율 {rateText(traffic.rate?.writePerSec)}</span>
      </div>
      <div className="pointer-events-auto rounded-md border border-amber-400 bg-amber-50/95 px-3 py-1.5 text-center shadow-xs">
        <span className="block text-[10px] font-medium text-amber-900">보류 중 요청 (실시간 · Worker)</span>
        <span className="font-mono text-sm font-bold tabular-nums text-amber-900">{countText(worker?.held_count)}</span>
        <span className="block font-mono text-[10px] text-amber-900/70">원본 처리 중 {countText(worker?.inflight_source)}</span>
      </div>
      <div className="pointer-events-auto rounded-md border border-ink/20 bg-white/95 px-3 py-1.5 text-right shadow-xs">
        <span className="block text-[10px] text-ink/55">읽기 (실시간 · Worker 누적)</span>
        <span className="font-mono text-sm font-bold tabular-nums">
          {countText(counts?.read_total)}
          <span className="ml-1 text-[10px] font-normal text-ink/55">실패 {countText(counts?.read_failed)}</span>
        </span>
        <span className="block font-mono text-[10px] text-ink/55">비율 {rateText(traffic.rate?.readPerSec)}</span>
      </div>
    </div>
  );
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

export function PhysicsControlsOverlay({
  mode,
  onChangeMode,
  measured,
  reportedServing,
  phaseView,
  replay,
  traffic,
  drivingCheckpoint,
  speedKmh,
  gear = "P",
  cameraMode = "chase",
  onChangeCameraMode,
  onUpdateDriveControl,
  onResetVehicle,
}: Props) {
  return (
    <div className="pointer-events-none absolute inset-0 flex flex-col justify-between p-3 text-ink select-none">
      {/* 상단 바: 모드 전환 & 단계별 제어 */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        {/* 좌측: Step 3 내부 모드 탭 */}
        <div className="pointer-events-auto inline-flex rounded-md border border-ink bg-white p-0.5 shadow-[2px_2px_0_0_var(--color-ink)]">
          <button
            type="button"
            onClick={() => onChangeMode("cutover_valve")}
            className={`rounded px-2.5 py-1 text-xs font-semibold transition-colors ${
              mode === "cutover_valve" ? "bg-ink text-paper" : "text-ink/70 hover:text-ink"
            }`}
          >
            전환 트래픽 (실시간 · 재생)
          </button>
          <button
            type="button"
            onClick={() => onChangeMode("driving_demo")}
            className={`rounded px-2.5 py-1 text-xs font-semibold transition-colors ${
              mode === "driving_demo" ? "bg-ink text-paper" : "text-ink/70 hover:text-ink"
            }`}
          >
            경영진 데모: 파이프라인 주행 (WASD)
          </button>
        </div>

        {/* 우측: 현재 모드별 액션 컨트롤 */}
        {mode === "cutover_valve" ? (
          <div className="pointer-events-auto flex max-w-sm flex-wrap items-center gap-2 rounded-md border border-ink/25 bg-white/95 px-2.5 py-1 text-xs shadow-xs">
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
            {replay.note && <span className="font-mono text-[10px] text-ink/60">{replay.note}</span>}
          </div>
        ) : (
          <div className="pointer-events-auto flex flex-wrap items-center gap-2 rounded-md border border-ink/25 bg-white/95 px-2.5 py-1 text-xs shadow-xs">
            {/* 카메라 시점 토글 */}
            <div className="flex items-center gap-1 rounded border border-ink/20 bg-paper p-0.5 text-[11px]">
              <button
                type="button"
                onClick={() => onChangeCameraMode?.("chase")}
                className={`rounded px-1.5 py-0.5 font-medium transition-all active:scale-[0.96] ${
                  cameraMode === "chase"
                    ? "bg-ink font-semibold text-paper shadow-xs"
                    : "text-ink/65 hover:text-ink"
                }`}
              >
                🎥 3인칭
              </button>
              <button
                type="button"
                onClick={() => onChangeCameraMode?.("cockpit")}
                className={`rounded px-1.5 py-0.5 font-medium transition-all active:scale-[0.96] ${
                  cameraMode === "cockpit"
                    ? "bg-ink font-semibold text-paper shadow-xs"
                    : "text-ink/65 hover:text-ink"
                }`}
              >
                🏎️ 운전석
              </button>
              <button
                type="button"
                onClick={() => onChangeCameraMode?.("topdown")}
                className={`rounded px-1.5 py-0.5 font-medium transition-all active:scale-[0.96] ${
                  cameraMode === "topdown"
                    ? "bg-ink font-semibold text-paper shadow-xs"
                    : "text-ink/65 hover:text-ink"
                }`}
              >
                🛰️ 탑뷰
              </button>
              <span className="hidden px-1 font-mono text-[10px] text-ink/40 sm:inline">(단축키 C)</span>
            </div>

            {/* 디지털 계기판 (기어 + 속도) */}
            <div className="flex items-center gap-1.5 rounded border border-ink/20 bg-white px-2 py-0.5">
              <div className="flex items-center gap-0.5 font-mono text-[10px] font-bold">
                {(["P", "R", "N", "D"] as const).map((g) => (
                  <span
                    key={g}
                    className={`rounded px-1 py-0.2 ${
                      gear === g
                        ? "bg-indigo-900 font-extrabold text-white"
                        : "text-ink/30"
                    }`}
                  >
                    {g}
                  </span>
                ))}
              </div>
              <span className="font-mono text-xs font-bold tabular-nums text-indigo-950">
                {speedKmh} <span className="text-[10px] font-normal text-ink/60">km/h</span>
              </span>
            </div>

            <button
              type="button"
              onClick={onResetVehicle}
              className="rounded border border-ink/25 bg-paper px-2 py-0.5 text-[11px] font-medium transition-transform hover:border-ink active:scale-[0.96]"
            >
              출발선 리셋
            </button>
          </div>
        )}
      </div>

      {/* 하단 바 */}
      {mode === "cutover_valve" ? (
        <div className="flex flex-col gap-2">
          <PhaseBar phaseView={phaseView} traffic={traffic} />
          <MeasuredFacts measured={measured} reportedServing={reportedServing} />
          <p className="pointer-events-auto rounded-md border border-ink/15 bg-white/90 px-3 py-1 font-mono text-[10px] text-ink/65 shadow-xs">
            {trafficNote(traffic, phaseView.phase)}
          </p>
          <WorkerCounters traffic={traffic} />
        </div>
      ) : (
        /* 주행 데모 모드 하단 HUD: 현재 통과 중인 스테이지 실측 정보 + 마우스/터치 페달 */
        <div className="flex flex-wrap items-end justify-between gap-2">
          {drivingCheckpoint && (
            <div className="pointer-events-auto max-w-sm rounded-lg border border-ink bg-white/95 p-3 shadow-[2px_2px_0_0_var(--color-ink)]">
              <div className="flex items-center justify-between gap-3 border-b border-ink/10 pb-1">
                <span className="font-mono text-xs font-bold">
                  체크포인트 {String(drivingCheckpoint.index).padStart(2, "0")} · {drivingCheckpoint.label}
                </span>
                <span className="font-mono text-[10px] font-semibold uppercase text-emerald-700">
                  {drivingCheckpoint.state}
                </span>
              </div>
              <dl className="mt-1.5 space-y-0.5 text-[11px]">
                {drivingCheckpoint.facts.slice(0, 3).map((f) => (
                  <div key={f.label} className="flex justify-between gap-3">
                    <dt className="text-ink/55">{f.label}</dt>
                    <dd className="truncate font-mono font-medium">{f.value}</dd>
                  </div>
                ))}
              </dl>
            </div>
          )}

          {/* 마우스/터치 주행 컨트롤 패드 */}
          <div className="pointer-events-auto flex items-center gap-1 rounded-lg border border-ink bg-white/95 p-1.5 shadow-[2px_2px_0_0_var(--color-ink)]">
            <button
              type="button"
              onMouseDown={() => onUpdateDriveControl({ left: true })}
              onMouseUp={() => onUpdateDriveControl({ left: false })}
              onMouseLeave={() => onUpdateDriveControl({ left: false })}
              onTouchStart={(e) => {
                e.preventDefault();
                onUpdateDriveControl({ left: true });
              }}
              onTouchEnd={(e) => {
                e.preventDefault();
                onUpdateDriveControl({ left: false });
              }}
              onTouchCancel={(e) => {
                e.preventDefault();
                onUpdateDriveControl({ left: false });
              }}
              className="rounded border border-ink/25 bg-paper px-2.5 py-1.5 font-mono text-xs font-bold transition-transform hover:border-ink active:scale-[0.96] active:bg-ink active:text-paper"
            >
              ◀ 좌
            </button>
            <button
              type="button"
              onMouseDown={() => onUpdateDriveControl({ forward: true })}
              onMouseUp={() => onUpdateDriveControl({ forward: false })}
              onMouseLeave={() => onUpdateDriveControl({ forward: false })}
              onTouchStart={(e) => {
                e.preventDefault();
                onUpdateDriveControl({ forward: true });
              }}
              onTouchEnd={(e) => {
                e.preventDefault();
                onUpdateDriveControl({ forward: false });
              }}
              onTouchCancel={(e) => {
                e.preventDefault();
                onUpdateDriveControl({ forward: false });
              }}
              className="rounded border border-ink bg-ink px-3 py-1.5 font-mono text-xs font-bold text-paper transition-transform active:scale-[0.96] active:opacity-80"
            >
              ▲ 전진 가속
            </button>
            <button
              type="button"
              onMouseDown={() => onUpdateDriveControl({ backward: true })}
              onMouseUp={() => onUpdateDriveControl({ backward: false })}
              onMouseLeave={() => onUpdateDriveControl({ backward: false })}
              onTouchStart={(e) => {
                e.preventDefault();
                onUpdateDriveControl({ backward: true });
              }}
              onTouchEnd={(e) => {
                e.preventDefault();
                onUpdateDriveControl({ backward: false });
              }}
              onTouchCancel={(e) => {
                e.preventDefault();
                onUpdateDriveControl({ backward: false });
              }}
              className="rounded border border-ink/25 bg-paper px-2.5 py-1.5 font-mono text-xs font-bold transition-transform hover:border-ink active:scale-[0.96] active:bg-ink active:text-paper"
            >
              ▼ 후진
            </button>
            <button
              type="button"
              onMouseDown={() => onUpdateDriveControl({ right: true })}
              onMouseUp={() => onUpdateDriveControl({ right: false })}
              onMouseLeave={() => onUpdateDriveControl({ right: false })}
              onTouchStart={(e) => {
                e.preventDefault();
                onUpdateDriveControl({ right: true });
              }}
              onTouchEnd={(e) => {
                e.preventDefault();
                onUpdateDriveControl({ right: false });
              }}
              onTouchCancel={(e) => {
                e.preventDefault();
                onUpdateDriveControl({ right: false });
              }}
              className="rounded border border-ink/25 bg-paper px-2.5 py-1.5 font-mono text-xs font-bold transition-transform hover:border-ink active:scale-[0.96] active:bg-ink active:text-paper"
            >
              우 ▶
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
