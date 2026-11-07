"use client";

import { OrbitControls, PerspectiveCamera } from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
import { useCallback, useEffect, useRef, useState } from "react";
import { cutoverOverlayFacts, cutoverSteps, servingSide } from "@/lib/dashboard";
import { replayPhaseAt, replayableSteps } from "@/lib/cutoverReplay";
import { liveTrafficPhase, totalRate, useEdgeLive } from "@/lib/edgeLive";
import type { StageReportCardData } from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";
import { CutoverPhysicsScene, type CutoverPhase } from "./CutoverPhysicsScene";
import {
  PhysicsControlsOverlay,
  type PhaseView,
  type ReplayControls,
  type Step3Mode,
  type TrafficView,
} from "./PhysicsControlsOverlay";
import {
  PipelineDrivingScene,
  type CameraMode,
  type DrivingControlState,
} from "./PipelineDrivingScene";

interface Props {
  run: RunStatus;
  initialMode?: Step3Mode;
}

// 풀(48개)과 구슬 수명으로 화면에 담을 수 있는 초당 구슬 수. 더 많으면 구슬 1개가 요청 여러 건을 뜻합니다.
const MAX_BALLS_PER_SEC = 6;
// 마지막 단계 뒤에 끝 상태를 잠시 더 보여 준 뒤 "재생 종료"로 표시합니다.
const REPLAY_END_PAUSE_MS = 1500;

type Replay = { key: string; index: number; isDone: boolean };

export default function PhysicsCanvasView({ run, initialMode = "cutover_valve" }: Props) {
  const [mode, setMode] = useState<Step3Mode>(initialMode);

  // 실시간 게이트 상태: Worker mode/next_origin 만 씁니다. 화면이 전환 뷰일 때만 폴링합니다.
  const { snapshot, rate, fetchError } = useEdgeLive(1500, mode === "cutover_valve");
  const worker = snapshot?.worker ?? null;
  const livePhase: CutoverPhase = liveTrafficPhase(worker);

  // 실측 기록 재생: 이 run 의 cutover_steps[].elapsed_ms 를 실제 시간(1배속) 그대로 다시 보여 줍니다.
  const rawSteps = cutoverSteps(run);
  const { steps: replaySteps, skipped: skippedSteps } = replayableSteps(rawSteps);
  const stepsKey = JSON.stringify(replaySteps);
  const [replay, setReplay] = useState<Replay | null>(null);
  // 단계 기록이 바뀌면 지난 재생은 버립니다 (렌더 중 파생, 효과에서 상태를 바꾸지 않음).
  const activeReplay = replay && replay.key === stepsKey ? replay : null;
  const timersRef = useRef<ReturnType<typeof setTimeout>[]>([]);

  const clearTimers = useCallback(() => {
    timersRef.current.forEach(clearTimeout);
    timersRef.current = [];
  }, []);

  useEffect(() => clearTimers, [clearTimers, stepsKey]);

  const startReplay = useCallback(() => {
    clearTimers();
    if (replaySteps.length === 0) return;
    setReplay({ key: stepsKey, index: -1, isDone: false });
    timersRef.current = replaySteps.map((step, i) =>
      setTimeout(() => setReplay({ key: stepsKey, index: i, isDone: false }), step.elapsedMs),
    );
    const lastMs = replaySteps[replaySteps.length - 1].elapsedMs;
    timersRef.current.push(
      setTimeout(
        () => setReplay({ key: stepsKey, index: replaySteps.length - 1, isDone: true }),
        lastMs + REPLAY_END_PAUSE_MS,
      ),
    );
  }, [clearTimers, replaySteps, stepsKey]);

  const exitReplay = useCallback(() => {
    clearTimers();
    setReplay(null);
  }, [clearTimers]);

  const phase: CutoverPhase = activeReplay ? replayPhaseAt(replaySteps, activeReplay.index) : livePhase;
  const currentStep = activeReplay && activeReplay.index >= 0 ? replaySteps[activeReplay.index] : null;

  const phaseView: PhaseView = activeReplay
    ? {
        phase,
        source: activeReplay.isDone ? "replay_done" : "replay",
        stepLabel: currentStep ? `${activeReplay.index + 1}/${replaySteps.length} ${currentStep.step} · ${currentStep.elapsedMs.toLocaleString("ko-KR")}ms` : `0/${replaySteps.length} 시작 전`,
      }
    : { phase, source: "live", stepLabel: null };

  let replayNote: string | null = null;
  if (rawSteps === null) replayNote = "이 run 에 전환 단계 기록이 없습니다 (미제공).";
  else if (rawSteps.length === 0) replayNote = "전환 단계가 보고됐지만 비어 있습니다.";
  else if (replaySteps.length === 0) replayNote = "단계에 경과 시간(elapsed_ms)이 없어 재생할 수 없습니다.";
  else if (skippedSteps > 0) replayNote = `경과 시간이 없는 단계 ${skippedSteps}개는 건너뜁니다.`;

  const replayControls: ReplayControls = {
    canReplay: replaySteps.length > 0,
    note: replayNote,
    isActive: activeReplay !== null,
    onStart: startReplay,
    onExit: exitReplay,
  };

  // 구슬은 실시간 보기에서만, 실측 요청 비율만큼 만듭니다. 재생 중에는 그때의 트래픽 기록이 없으므로 만들지 않습니다.
  const measuredRate = totalRate(rate);
  const ballsPerSec =
    activeReplay || measuredRate === null ? null : Math.min(measuredRate, MAX_BALLS_PER_SEC);
  const traffic: TrafficView = {
    worker,
    fetchError,
    rate,
    requestsPerBall: measuredRate !== null && measuredRate > MAX_BALLS_PER_SEC ? measuredRate / MAX_BALLS_PER_SEC : null,
    isReplay: activeReplay !== null,
  };

  // 주행 데모 상태
  const [drivingCheckpoint, setDrivingCheckpoint] = useState<StageReportCardData | null>(null);
  const [speedKmh, setSpeedKmh] = useState(0);
  const [gear, setGear] = useState<"P" | "D" | "R">("P");
  const [cameraMode, setCameraMode] = useState<CameraMode>("chase");
  const [driveControls, setDriveControls] = useState<DrivingControlState>({
    forward: false,
    backward: false,
    left: false,
    right: false,
    brake: false,
    resetSignal: 0,
  });

  const handleUpdateDriveControl = (patch: Partial<DrivingControlState>) => {
    setDriveControls((prev) => ({ ...prev, ...patch }));
  };

  const handleResetVehicle = () => {
    setDriveControls((prev) => ({ ...prev, resetSignal: prev.resetSignal + 1 }));
  };

  const handleCheckpointChange = useCallback(
    (cp: StageReportCardData, spd: number, nextGear?: "P" | "D" | "R") => {
      setDrivingCheckpoint(cp);
      setSpeedKmh(spd);
      if (nextGear) setGear(nextGear);
    },
    []
  );

  return (
    <div className="relative h-full w-full overflow-hidden bg-paper select-none">
      <Canvas
        shadows
        dpr={[1, 2]}
        gl={{ antialias: true }}
        className="h-full w-full"
      >
        <color attach="background" args={["#f6f4ef"]} />

        {mode === "cutover_valve" ? (
          <>
            <PerspectiveCamera makeDefault position={[0, 0.9, 6.9]} fov={40} />
            <OrbitControls
              enablePan={false}
              enableZoom={true}
              minDistance={4.5}
              maxDistance={10}
              minPolarAngle={Math.PI / 6}
              maxPolarAngle={Math.PI / 2 - 0.05}
              dampingFactor={0.08}
            />
            <ambientLight intensity={1.15} />
            <directionalLight position={[6, 10, 6]} intensity={1.1} castShadow />
            <directionalLight position={[-6, 6, -4]} intensity={0.45} />

            <CutoverPhysicsScene run={run} phase={phase} ballsPerSec={ballsPerSec} />
          </>
        ) : (
          <>
            <PerspectiveCamera makeDefault position={[0, 3.2, 7.2]} fov={44} />
            <PipelineDrivingScene
              run={run}
              controls={driveControls}
              cameraMode={cameraMode}
              onToggleCameraMode={setCameraMode}
              onCheckpointChange={handleCheckpointChange}
            />
          </>
        )}
      </Canvas>

      <PhysicsControlsOverlay
        mode={mode}
        onChangeMode={setMode}
        measured={cutoverOverlayFacts(run)}
        reportedServing={servingSide(run)}
        phaseView={phaseView}
        replay={replayControls}
        traffic={traffic}
        drivingCheckpoint={drivingCheckpoint}
        speedKmh={speedKmh}
        gear={gear}
        cameraMode={cameraMode}
        onChangeCameraMode={setCameraMode}
        onUpdateDriveControl={handleUpdateDriveControl}
        onResetVehicle={handleResetVehicle}
      />
    </div>
  );
}
