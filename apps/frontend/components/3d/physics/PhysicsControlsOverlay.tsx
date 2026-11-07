"use client";

import { countTone, type CutoverOverlayFacts, type CutoverSecondsSource } from "@/lib/dashboard";
import type { StageReportCardData } from "@/lib/topology3d";
import type { CutoverPhase, PhysicsMetrics } from "./CutoverPhysicsScene";
import type { CameraMode, DrivingControlState } from "./PipelineDrivingScene";

export type Step3Mode = "cutover_valve" | "driving_demo";

interface Props {
  mode: Step3Mode;
  onChangeMode: (mode: Step3Mode) => void;
  // 이 run 의 보고값 (애니메이션과 별개)
  measured: CutoverOverlayFacts;
  // Cutover Valve Mode props
  metrics: PhysicsMetrics;
  phase: CutoverPhase;
  isSequenceRunning: boolean;
  onRunSequence: () => void;
  onSelectPhase: (phase: CutoverPhase) => void;
  trafficRps: number;
  onChangeRps: (rps: number) => void;
  onResetCounts: () => void;
  // Driving Demo Mode props
  drivingCheckpoint: StageReportCardData | null;
  speedKmh: number;
  gear?: "P" | "D" | "R";
  cameraMode?: CameraMode;
  onChangeCameraMode?: (mode: CameraMode) => void;
  onUpdateDriveControl: (patch: Partial<DrivingControlState>) => void;
  onResetVehicle: () => void;
}

const PHASE_STEPS: { key: CutoverPhase; label: string }[] = [
  { key: "aws_serving", label: "0. AWS 서빙" },
  { key: "hold", label: "1. 요청 보류(Hold)" },
  { key: "verify", label: "2. 최종 반영 확인" },
  { key: "switch", label: "3. 경로 변경(GCP)" },
  { key: "gcp_serving", label: "4. 보류 해제·완료" },
];

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
      {value !== null ? `${value}건` : "미제공"}
    </span>
  );
}

function stepsText(steps: CutoverOverlayFacts["steps"]): string {
  if (!steps || steps.length === 0) return "미제공";
  return steps
    .map((s) => `${s.step} ${s.elapsedMs !== null ? `${s.elapsedMs}ms` : "미제공"}`)
    .join(" · ");
}

// 이 run 이 실제로 보고한 값만 보여 줍니다. 위 애니메이션 단계와 대응시키지 않습니다.
function MeasuredFacts({ measured }: { measured: CutoverOverlayFacts }) {
  const { cutoverSeconds, cutoverSecondsSource } = measured;
  return (
    <div className="pointer-events-auto rounded-md border border-ink/20 bg-white/95 px-3 py-1 font-mono text-[10px] text-ink/75 shadow-xs">
      <p className="flex flex-wrap items-center gap-x-1">
        {measured.simulated && (
          <span className="rounded border border-amber-400 bg-amber-50 px-1 font-semibold text-amber-900">
            시뮬레이션 값
          </span>
        )}
        <span>
          이 run 보고값: 전환 소요 {cutoverSeconds !== null ? `${cutoverSeconds.toFixed(1)}초` : "미제공"} (
          {cutoverSecondsSource ? SECONDS_SOURCE_LABEL[cutoverSecondsSource] : "출처 미제공"}) · 읽기 실패{" "}
          <FailureCount value={measured.failedRequests} /> (프로버) · 쓰기 실패{" "}
          <FailureCount value={measured.writeFailedRequests} /> (Worker)
        </span>
      </p>
      <p>전환 시작 후 경과: {stepsText(measured.steps)}</p>
    </div>
  );
}

export function PhysicsControlsOverlay({
  mode,
  onChangeMode,
  measured,
  metrics,
  phase,
  isSequenceRunning,
  onRunSequence,
  onSelectPhase,
  trafficRps,
  onChangeRps,
  onResetCounts,
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
            물리 트래픽 전환 (Hold → Switch)
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
          <div className="pointer-events-auto flex flex-wrap items-center gap-2 rounded-md border border-ink/25 bg-white/95 px-2.5 py-1 text-xs shadow-xs">
            <button
              type="button"
              disabled={isSequenceRunning}
              onClick={onRunSequence}
              className="rounded bg-ink px-2.5 py-1 text-xs font-semibold text-paper transition-transform hover:-translate-y-px disabled:opacity-50"
            >
              {isSequenceRunning ? "4단계 전환 시퀀스 진행 중…" : "▶ 4단계 무중단 전환 리허설 실행"}
            </button>
            <label className="flex items-center gap-1.5 font-mono text-[11px] text-ink/70">
              <span>유입</span>
              <input
                type="range"
                min={5}
                max={24}
                value={trafficRps}
                onChange={(e) => onChangeRps(Number(e.target.value))}
                className="h-1.5 w-16 cursor-pointer accent-ink"
              />
              <span className="w-12 text-right font-semibold text-ink">{trafficRps} req/s</span>
            </label>
            <button
              type="button"
              onClick={onResetCounts}
              className="rounded border border-ink/20 bg-paper px-2 py-0.5 text-[11px] font-medium hover:border-ink"
            >
              초기화
            </button>
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
          {/* 전환 순서 막대: 애니메이션 단계만 표시 (실측 아님) */}
          <div className="pointer-events-auto flex flex-wrap items-center justify-between gap-1 rounded-md border border-ink/20 bg-white/95 px-3 py-1.5 text-[11px] font-mono shadow-xs">
            <span className="flex items-center gap-1.5 font-semibold text-ink">
              <span className="inline-block h-2 w-2 rounded-full bg-ink/40" />
              Cloudflare 4단계 전환 순서
              <span className="rounded border border-ink/25 bg-paper px-1.5 py-0.5 text-[10px] font-medium text-ink/70">
                리허설 애니메이션, 실측 아님
              </span>
            </span>
            <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-ink/65">
              <span className={`rounded px-1.5 py-0.5 transition-colors ${phase === "hold" ? "bg-amber-100 font-bold text-amber-900 border border-amber-300" : ""}`}>
                ① 보류(Hold)
              </span>
              <span>→</span>
              <span className={`rounded px-1.5 py-0.5 transition-colors ${phase === "verify" ? "bg-indigo-100 font-bold text-indigo-900 border border-indigo-300" : ""}`}>
                ② 반영 확인
              </span>
              <span>→</span>
              <span className={`rounded px-1.5 py-0.5 transition-colors ${phase === "switch" ? "bg-purple-100 font-bold text-purple-900 border border-purple-300" : ""}`}>
                ③ 경로 변경
              </span>
              <span>→</span>
              <span className={`rounded px-1.5 py-0.5 transition-colors ${phase === "gcp_serving" ? "bg-emerald-100 font-bold text-emerald-900 border border-emerald-300" : ""}`}>
                ④ 보류 해제
              </span>
            </div>
          </div>

          <MeasuredFacts measured={measured} />

          {/* 4단계 전환 시퀀스 스텝 버튼 */}
          <div className="pointer-events-auto flex flex-wrap items-center justify-center gap-1 rounded-md border border-ink/20 bg-white/95 p-1.5 shadow-xs">
            {PHASE_STEPS.map((st) => {
              const active = phase === st.key;
              return (
                <button
                  key={st.key}
                  type="button"
                  onClick={() => onSelectPhase(st.key)}
                  className={`rounded px-2 py-0.5 font-mono text-[11px] transition-all active:scale-[0.96] ${
                    active
                      ? "bg-ink font-semibold text-paper shadow-xs"
                      : "bg-paper text-ink/65 hover:text-ink"
                  }`}
                >
                  {st.label}
                </button>
              );
            })}
          </div>

          {/* 애니메이션 카운터 (실측 아님) */}
          <div className="grid grid-cols-3 gap-2">
            <div className="pointer-events-auto rounded-md border border-ink/20 bg-white/95 px-3 py-1.5 shadow-xs">
              <span className="block text-[10px] text-ink/55">원본 AWS 처리 (애니메이션)</span>
              <span className="font-mono text-base font-bold tabular-nums">
                {metrics.awsCount.toLocaleString("ko-KR")}
                <span className="ml-1 text-[10px] font-normal text-ink/50">건</span>
              </span>
            </div>

            <div className="pointer-events-auto rounded-md border border-amber-400 bg-amber-50/95 px-3 py-1.5 text-center shadow-xs">
              <span className="block text-[10px] font-medium text-amber-900">
                Cloudflare 보류 버퍼 (애니메이션)
              </span>
              <span className="font-mono text-base font-bold tabular-nums text-amber-900">
                {metrics.bufferedCount}
                <span className="ml-1 text-[10px] font-normal">건 대기</span>
              </span>
            </div>

            <div className="pointer-events-auto rounded-md border border-emerald-600/40 bg-white/95 px-3 py-1.5 text-right shadow-xs">
              <span className="block text-[10px] text-emerald-800">목적지 GCP 처리 (애니메이션)</span>
              <span className="font-mono text-base font-bold tabular-nums text-emerald-700">
                {metrics.gcpCount.toLocaleString("ko-KR")}
                <span className="ml-1 text-[10px] font-normal text-ink/50">건</span>
              </span>
            </div>
          </div>
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
