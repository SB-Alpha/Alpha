"use client";

import { OrbitControls, PerspectiveCamera } from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
import { useCallback, useEffect, useRef, useState } from "react";
import { cutoverOverlayFacts, servingSide } from "@/lib/dashboard";
import type { StageReportCardData } from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";
import {
  CutoverPhysicsScene,
  type CutoverPhase,
  type PhysicsMetrics,
} from "./CutoverPhysicsScene";
import { PhysicsControlsOverlay, type Step3Mode } from "./PhysicsControlsOverlay";
import {
  PipelineDrivingScene,
  type CameraMode,
  type DrivingControlState,
} from "./PipelineDrivingScene";

interface Props {
  run: RunStatus;
  initialMode?: Step3Mode;
  sequenceTrigger?: number;
}

export default function PhysicsCanvasView({
  run,
  initialMode = "cutover_valve",
  sequenceTrigger = 0,
}: Props) {
  // 승인만으로는 트래픽이 넘어갔다고 보지 않습니다. 보고된 serving 지표만 믿습니다.
  const defaultPhase: CutoverPhase = servingSide(run) === "gcp" ? "gcp_serving" : "aws_serving";

  const [mode, setMode] = useState<Step3Mode>(initialMode);
  const [phase, setPhase] = useState<CutoverPhase>(defaultPhase);
  const [isSequenceRunning, setIsSequenceRunning] = useState(false);
  const [trafficRps, setTrafficRps] = useState(11);
  const [resetToken, setResetToken] = useState(0);
  const [metrics, setMetrics] = useState<PhysicsMetrics>({
    awsCount: 0,
    gcpCount: 0,
    bufferedCount: 0,
    droppedCount: 0,
    phase: defaultPhase,
  });

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

  const timersRef = useRef<ReturnType<typeof setTimeout>[]>([]);

  const clearTimers = useCallback(() => {
    timersRef.current.forEach(clearTimeout);
    timersRef.current = [];
  }, []);

  // 4단계 무중단 전환 리허설 시퀀스 (요청 보류 → 최종 반영 확인 → 경로 변경 → 보류 해제)
  const runFourStepCutover = useCallback(() => {
    clearTimers();
    setIsSequenceRunning(true);
    setPhase("aws_serving");

    const t1 = setTimeout(() => setPhase("hold"), 600);
    const t2 = setTimeout(() => setPhase("verify"), 2200);
    const t3 = setTimeout(() => setPhase("switch"), 3500);
    const t4 = setTimeout(() => {
      setPhase("gcp_serving");
      setIsSequenceRunning(false);
    }, 4800);

    timersRef.current = [t1, t2, t3, t4];
  }, [clearTimers]);

  useEffect(() => {
    if (sequenceTrigger <= 0) return;
    const t0 = setTimeout(() => {
      runFourStepCutover();
    }, 0);
    return () => clearTimeout(t0);
  }, [sequenceTrigger, runFourStepCutover]);

  useEffect(() => {
    return () => clearTimers();
  }, [clearTimers]);

  const handleSelectPhase = (nextPhase: CutoverPhase) => {
    clearTimers();
    setIsSequenceRunning(false);
    setPhase(nextPhase);
  };

  const handleResetCounts = () => {
    setResetToken((n) => n + 1);
  };

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

            <CutoverPhysicsScene
              run={run}
              phase={phase}
              trafficRps={trafficRps}
              resetToken={resetToken}
              onMetricsUpdate={setMetrics}
            />
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
        metrics={metrics}
        phase={phase}
        isSequenceRunning={isSequenceRunning}
        onRunSequence={runFourStepCutover}
        onSelectPhase={handleSelectPhase}
        trafficRps={trafficRps}
        onChangeRps={setTrafficRps}
        onResetCounts={handleResetCounts}
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
