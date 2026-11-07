"use client";

import { ContactShadows, Line, OrbitControls, PerspectiveCamera } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef, type ComponentRef } from "react";
import * as THREE from "three";
import type { RunStatus } from "@/lib/workflows";
import { StageCdc } from "./StageCdc";
import { StageCutover } from "./StageCutover";
import { StageDiscovery } from "./StageDiscovery";
import { StagePlanning } from "./StagePlanning";
import { StageProvisioning } from "./StageProvisioning";

type OrbitControlsHandle = ComponentRef<typeof OrbitControls>;

export type JourneyViewMode = 0 | 1 | 2 | 3 | 4 | 5 | "overview";

interface Props {
  run: RunStatus;
  activeView: JourneyViewMode;
  flyTrigger: number;
  zoomScale: number;
  onSelectStage: (stageIdx: number) => void;
}

const STAGE_KEYFRAMES: Record<number, { cam: [number, number, number]; look: [number, number, number] }> = {
  0: { cam: [-10.5, 2.15, 4.5], look: [-10.5, 0.35, 0] },
  1: { cam: [-6.3, 2.15, 4.5], look: [-6.3, 0.35, 0] },
  2: { cam: [-2.1, 2.25, 4.5], look: [-2.1, 0.35, 0] },
  3: { cam: [2.1, 2.15, 4.5], look: [2.1, 0.35, 0] },
  4: { cam: [6.3, 2.15, 4.5], look: [6.3, 0.35, 0] },
  5: { cam: [10.5, 2.25, 4.7], look: [10.5, 0.45, 0] },
};

const OVERVIEW_KEYFRAME = {
  cam: [0, 6.2, 22.5] as [number, number, number],
  look: [0, 0.25, 0] as [number, number, number],
};

export function MigrationJourneyScene({
  run,
  activeView,
  flyTrigger,
  zoomScale,
  onSelectStage,
}: Props) {
  const { camera } = useThree();
  const controlsRef = useRef<OrbitControlsHandle | null>(null);
  const isFlyingRef = useRef(true);

  const desiredCamPos = useMemo(() => new THREE.Vector3(-10.5, 2.15, 4.5), []);
  const desiredLookAt = useMemo(() => new THREE.Vector3(-10.5, 0.35, 0), []);

  useEffect(() => {
    const kf = activeView === "overview" ? OVERVIEW_KEYFRAME : (STAGE_KEYFRAMES[activeView] ?? STAGE_KEYFRAMES[0]);
    const lookVec = new THREE.Vector3(...kf.look);
    const camVec = new THREE.Vector3(...kf.cam);

    // zoomScale 적용 (look 지점 기준 거리 스케일링)
    const dir = camVec.clone().sub(lookVec).multiplyScalar(zoomScale);
    desiredLookAt.copy(lookVec);
    desiredCamPos.copy(lookVec).add(dir);
    isFlyingRef.current = true;
  }, [activeView, flyTrigger, zoomScale, desiredCamPos, desiredLookAt]);

  const spinePoints = useMemo(
    () => [new THREE.Vector3(-11.8, -0.35, 0), new THREE.Vector3(11.8, -0.35, 0)],
    []
  );

  useFrame((_, delta) => {
    if (!isFlyingRef.current) return;

    const step = Math.min(1, delta * 7.5);
    camera.position.lerp(desiredCamPos, step);

    if (controlsRef.current) {
      controlsRef.current.target.lerp(desiredLookAt, step);
      controlsRef.current.update();
    } else {
      camera.lookAt(desiredLookAt);
    }

    const camDist = camera.position.distanceTo(desiredCamPos);
    const targetDist = controlsRef.current
      ? controlsRef.current.target.distanceTo(desiredLookAt)
      : 0;

    if (camDist < 0.015 && targetDist < 0.015) {
      camera.position.copy(desiredCamPos);
      if (controlsRef.current) {
        controlsRef.current.target.copy(desiredLookAt);
        controlsRef.current.update();
      }
      isFlyingRef.current = false;
    }
  });

  return (
    <>
      <color attach="background" args={["#f6f4ef"]} />

      <PerspectiveCamera makeDefault position={[-10.5, 2.15, 4.5]} fov={38} />

      {/* 마우스 드래그 회전·패닝 지원 (휠 줌은 페이지 스크롤 방해 방지를 위해 비활성화) */}
      <OrbitControls
        ref={controlsRef}
        makeDefault
        enableZoom={false}
        enablePan={true}
        enableRotate={true}
        minPolarAngle={0.18}
        maxPolarAngle={Math.PI / 2 - 0.05}
        dampingFactor={0.08}
        onStart={() => {
          isFlyingRef.current = false;
        }}
      />

      {/* 건축 스튜디오 조명 */}
      <ambientLight intensity={1.15} />
      <directionalLight position={[10, 12, 8]} intensity={1.1} castShadow />
      <directionalLight position={[-10, 6, -6]} intensity={0.45} />

      {/* 전체 바닥 그리드 & 연결 라인 */}
      <group position={[0, -0.43, 0]}>
        <gridHelper args={[32, 32, 0xd6d3cd, 0xe7e5e0]} />
        <ContactShadows
          position={[0, 0.01, 0]}
          opacity={0.22}
          scale={28}
          blur={2.0}
          far={2.5}
          color="#1c1917"
        />
      </group>
      <Line points={spinePoints} color="#1c1917" lineWidth={1.5} dashed dashSize={0.3} gapSize={0.2} />

      {/* 6개 실제 마이그레이션 단계 (클릭 시 해당 스테이지로 즉시 포커스 이동) */}
      <StageDiscovery
        run={run}
        isActive={activeView === 0}
        onSelect={() => onSelectStage(0)}
      />
      <StagePlanning
        run={run}
        isActive={activeView === 1}
        onSelect={() => onSelectStage(1)}
      />
      <StageProvisioning
        run={run}
        isActive={activeView === 2}
        onSelect={() => onSelectStage(2)}
      />
      <StageCdc
        run={run}
        isActive={activeView === 3}
        onSelect={() => onSelectStage(3)}
      />
      <StageCutover
        run={run}
        activeIdx={activeView}
        onSelectStage={onSelectStage}
      />
    </>
  );
}
