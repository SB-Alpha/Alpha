"use client";

import { Edges } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import { extractCdcArcData } from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
  isActive?: boolean;
  onSelect?: () => void;
}

const PACKET_COUNT = 6;

export function StageCdc({ run, isActive: isStageActive = false, onSelect }: Props) {
  const cdc = extractCdcArcData(run);
  const packetsRef = useRef<(THREE.Mesh | null)[]>([]);
  const offsets = useMemo(
    () => Array.from({ length: PACKET_COUNT }, (_, i) => i / PACKET_COUNT),
    []
  );
  const progressRef = useRef<number[]>([...offsets]);

  const isFlowing =
    cdc.stageState === "running" || cdc.stageState === "succeeded" || cdc.copyPercent === 100;

  useFrame((_, delta) => {
    if (!isFlowing) return;
    const speed = cdc.stageState === "running" ? 0.5 : 0.22;
    progressRef.current.forEach((t, i) => {
      const nextT = (t + delta * speed) % 1.0;
      progressRef.current[i] = nextT;
      const mesh = packetsRef.current[i];
      if (mesh) {
        mesh.position.x = -0.9 + nextT * 1.8;
      }
    });
  });

  return (
    <group
      position={[2.1, 0, 0]}
      onClick={(e) => {
        e.stopPropagation();
        onSelect?.();
      }}
      onPointerOver={(e) => {
        e.stopPropagation();
        document.body.style.cursor = "pointer";
      }}
      onPointerOut={() => {
        document.body.style.cursor = "auto";
      }}
    >
      {isStageActive && (
        <mesh position={[0, -0.4, 0]}>
          <boxGeometry args={[3.04, 0.06, 2.24]} />
          <meshBasicMaterial color="#3730a3" />
        </mesh>
      )}

      {/* 베이스 플랫폼 */}
      <mesh position={[0, -0.35, 0]} receiveShadow>
        <boxGeometry args={[2.8, 0.14, 2.0]} />
        <meshStandardMaterial color={isStageActive ? "#f5f5f4" : "#e7e5e0"} roughness={0.8} />
        <Edges color={isStageActive ? "#3730a3" : "#1c1917"} threshold={15} />
      </mesh>

      {/* 좌측 Source DB 실린더 */}
      <mesh position={[-1.0, 0.25, 0]} castShadow receiveShadow>
        <cylinderGeometry args={[0.36, 0.36, 1.05, 20]} />
        <meshStandardMaterial color="#ffffff" roughness={0.5} />
        <Edges color="#1c1917" threshold={15} />
      </mesh>

      {/* 우측 Target DB 실린더 */}
      <mesh position={[1.0, 0.25, 0]} castShadow receiveShadow>
        <cylinderGeometry args={[0.36, 0.36, 1.05, 20]} />
        <meshStandardMaterial
          color={cdc.copyPercent === 100 ? "#047857" : "#ffffff"}
          roughness={0.5}
        />
        <Edges color="#1c1917" threshold={15} />
      </mesh>

      {/* CDC 스트림 레일 */}
      <mesh position={[0, 0.25, 0]}>
        <boxGeometry args={[1.3, 0.04, 0.16]} />
        <meshStandardMaterial color="#1c1917" />
      </mesh>

      {/* 패킷 이동 */}
      {isFlowing &&
        offsets.map((_, i) => (
          <mesh
            key={i}
            ref={(el) => {
              packetsRef.current[i] = el;
            }}
            position={[0, 0.34, 0]}
          >
            <boxGeometry args={[0.12, 0.12, 0.12]} />
            <meshStandardMaterial color="#3730a3" />
            <Edges color="#1c1917" />
          </mesh>
        ))}
    </group>
  );
}
