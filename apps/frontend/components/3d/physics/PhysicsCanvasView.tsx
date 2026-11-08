"use client";

import { OrbitControls, PerspectiveCamera } from "@react-three/drei";
import { Canvas, useThree } from "@react-three/fiber";
import { useCallback, useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { cutoverOverlayFacts, cutoverSteps, servingSide } from "@/lib/dashboard";
import { replayPhaseAt, replayableSteps } from "@/lib/cutoverReplay";
import { liveTrafficPhase, totalRate, useEdgeLive } from "@/lib/edgeLive";
import type { RunStatus } from "@/lib/workflows";
import { CutoverPhysicsScene, type CutoverPhase } from "./CutoverPhysicsScene";
import {
  CutoverInfoPanel,
  CutoverTopBar,
  type PhaseView,
  type ReplayControls,
  type TrafficView,
} from "./PhysicsControlsOverlay";

interface Props {
  run: RunStatus;
  /** 전체화면처럼 부모 높이를 채울 때. 아니면 3D 영역 높이를 고정합니다. */
  fill?: boolean;
}

// 풀(48개)과 구슬 수명으로 화면에 담을 수 있는 초당 구슬 수. 더 많으면 구슬 1개가 요청 여러 건을 뜻합니다.
const MAX_BALLS_PER_SEC = 6;
// 마지막 단계 뒤에 끝 상태를 잠시 더 보여 준 뒤 "재생 종료"로 표시합니다.
const REPLAY_END_PAUSE_MS = 1500;

type Replay = { key: string; index: number; isDone: boolean };

// 카메라는 장면 가운데(y 0.85)를 봅니다. 장면은 y -1.3(수집통 바닥) ~ 3.0(보류 슈트 위)입니다.
const CAMERA_TARGET: [number, number, number] = [0, 0.85, 0];
const CAMERA_POSITION: [number, number, number] = [0, 1.5, 8.6];
const CAMERA_FOV = 42;
// 좌우 수집통(x ±3.3)과 그 아래 라벨까지 담아야 하는 가로 반폭 (월드 단위)
const SCENE_HALF_WIDTH = 4.7;

/** 캔버스가 좁으면(세로로 긴 화면) 가로가 잘리지 않을 만큼 카메라를 뒤로 뺍니다. */
function FitSceneWidth() {
  const camera = useThree((s) => s.camera);
  const aspect = useThree((s) => s.size.width / Math.max(1, s.size.height));
  useEffect(() => {
    if (!(camera instanceof THREE.PerspectiveCamera)) return;
    const target = new THREE.Vector3(...CAMERA_TARGET);
    const base = new THREE.Vector3(...CAMERA_POSITION).distanceTo(target);
    const halfFov = THREE.MathUtils.degToRad(camera.fov / 2);
    const distance = Math.max(base, SCENE_HALF_WIDTH / (Math.tan(halfFov) * aspect));
    const direction = camera.position.clone().sub(target).normalize();
    camera.position.copy(target.add(direction.multiplyScalar(distance)));
    camera.updateProjectionMatrix();
  }, [camera, aspect]);
  return null;
}

// 상태 바(위)·3D 장면(가운데)·정보 패널(아래)을 세로로 쌓습니다. 정보 상자는 장면 위에 겹치지 않습니다.
export default function PhysicsCanvasView({ run, fill = false }: Props) {
  // 실시간 게이트 상태: Worker mode/next_origin 만 씁니다.
  const { snapshot, rate, fetchError } = useEdgeLive(1500, true);
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
        stepLabel: currentStep
          ? `${activeReplay.index + 1}/${replaySteps.length} ${currentStep.step} · ${currentStep.elapsedMs.toLocaleString("ko-KR")}ms`
          : `0/${replaySteps.length} 시작 전`,
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
  const ballsPerSec = activeReplay || measuredRate === null ? null : Math.min(measuredRate, MAX_BALLS_PER_SEC);
  const traffic: TrafficView = {
    worker,
    fetchError,
    rate,
    requestsPerBall: measuredRate !== null && measuredRate > MAX_BALLS_PER_SEC ? measuredRate / MAX_BALLS_PER_SEC : null,
    isReplay: activeReplay !== null,
  };

  return (
    <div className={`flex w-full flex-col bg-paper select-none ${fill ? "h-full" : ""}`}>
      <CutoverTopBar phaseView={phaseView} traffic={traffic} replay={replayControls} />

      <div className={`relative w-full ${fill ? "min-h-[360px] flex-1" : "h-[380px] sm:h-[460px] lg:h-[520px]"}`}>
        <Canvas shadows dpr={[1, 2]} gl={{ antialias: true }} className="h-full w-full">
          <color attach="background" args={["#f6f4ef"]} />
          <PerspectiveCamera makeDefault position={CAMERA_POSITION} fov={CAMERA_FOV} />
          <FitSceneWidth />
          <OrbitControls
            target={CAMERA_TARGET}
            enablePan={false}
            enableZoom={true}
            minDistance={4.5}
            maxDistance={18}
            minPolarAngle={Math.PI / 6}
            maxPolarAngle={Math.PI / 2 - 0.05}
            dampingFactor={0.08}
          />
          <ambientLight intensity={1.15} />
          <directionalLight position={[6, 10, 6]} intensity={1.1} castShadow />
          <directionalLight position={[-6, 6, -4]} intensity={0.45} />

          <CutoverPhysicsScene run={run} phase={phase} ballsPerSec={ballsPerSec} />
        </Canvas>
      </div>

      <CutoverInfoPanel
        phaseView={phaseView}
        traffic={traffic}
        measured={cutoverOverlayFacts(run)}
        reportedServing={servingSide(run)}
      />
    </div>
  );
}
