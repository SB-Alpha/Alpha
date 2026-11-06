"use client";

import { ContactShadows, Line, useScroll } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import type { RunStatus } from "@/lib/workflows";
import { StageCdc } from "./StageCdc";
import { StageCutover } from "./StageCutover";
import { StageDiscovery } from "./StageDiscovery";
import { StagePlanning } from "./StagePlanning";
import { StageProvisioning } from "./StageProvisioning";

interface Props {
  run: RunStatus;
  onActiveStageChange?: (stageIdx: number) => void;
}

const KEYFRAMES = [
  { offset: 0.0, cam: new THREE.Vector3(-15, 2.25, 5.2), look: new THREE.Vector3(-15, 0.4, 0) },
  { offset: 0.2, cam: new THREE.Vector3(-9, 2.25, 5.2), look: new THREE.Vector3(-9, 0.4, 0) },
  { offset: 0.4, cam: new THREE.Vector3(-3, 2.35, 5.2), look: new THREE.Vector3(-3, 0.4, 0) },
  { offset: 0.6, cam: new THREE.Vector3(3, 2.25, 5.2), look: new THREE.Vector3(3, 0.4, 0) },
  { offset: 0.8, cam: new THREE.Vector3(9, 2.25, 5.2), look: new THREE.Vector3(9, 0.4, 0) },
  { offset: 1.0, cam: new THREE.Vector3(15, 2.35, 5.4), look: new THREE.Vector3(15, 0.5, 0) },
];

export function MigrationJourneyScene({ run, onActiveStageChange }: Props) {
  const scroll = useScroll();
  const { camera } = useThree();

  const currentLookAt = useRef(new THREE.Vector3(-15, 0.4, 0));
  const lastStageIdx = useRef(0);
  const targetCamPos = useMemo(() => new THREE.Vector3(), []);
  const targetLookAt = useMemo(() => new THREE.Vector3(), []);

  const spinePoints = useMemo(
    () => [new THREE.Vector3(-15, -0.35, 0), new THREE.Vector3(15, -0.35, 0)],
    []
  );

  useFrame((_, delta) => {
    const offset = Math.min(1.0, Math.max(0.0, scroll.offset));

    const stageIdx = Math.min(5, Math.round(offset * 5));
    if (stageIdx !== lastStageIdx.current) {
      lastStageIdx.current = stageIdx;
      onActiveStageChange?.(stageIdx);
    }

    let startIdx = 0;
    for (let i = 0; i < KEYFRAMES.length - 1; i++) {
      if (offset >= KEYFRAMES[i].offset && offset <= KEYFRAMES[i + 1].offset) {
        startIdx = i;
        break;
      }
    }
    const endIdx = Math.min(startIdx + 1, KEYFRAMES.length - 1);
    const kfStart = KEYFRAMES[startIdx];
    const kfEnd = KEYFRAMES[endIdx];

    const range = kfEnd.offset - kfStart.offset;
    const localT = range > 0 ? (offset - kfStart.offset) / range : 0;

    targetCamPos.lerpVectors(kfStart.cam, kfEnd.cam, localT);
    targetLookAt.lerpVectors(kfStart.look, kfEnd.look, localT);

    camera.position.lerp(targetCamPos, Math.min(1, delta * 5.0));
    currentLookAt.current.lerp(targetLookAt, Math.min(1, delta * 5.0));
    camera.lookAt(currentLookAt.current);
  });

  return (
    <>
      <color attach="background" args={["#f6f4ef"]} />

      {/* 건축 스튜디오 조명 */}
      <ambientLight intensity={1.15} />
      <directionalLight position={[10, 12, 8]} intensity={1.1} castShadow />
      <directionalLight position={[-10, 6, -6]} intensity={0.45} />

      {/* 전체 바닥 그리드 & 연결 라인 */}
      <group position={[0, -0.43, 0]}>
        <gridHelper args={[44, 44, 0xd6d3cd, 0xe7e5e0]} />
        <ContactShadows
          position={[0, 0.01, 0]}
          opacity={0.22}
          scale={38}
          blur={2.0}
          far={2.5}
          color="#1c1917"
        />
      </group>
      <Line points={spinePoints} color="#1c1917" lineWidth={1.5} dashed dashSize={0.3} gapSize={0.2} />

      {/* 6개 실제 마이그레이션 단계 (01 Discovery ~ 06 Cutover) */}
      <StageDiscovery run={run} />
      <StagePlanning run={run} />
      <StageProvisioning run={run} />
      <StageCdc run={run} />
      <StageCutover run={run} />
    </>
  );
}
