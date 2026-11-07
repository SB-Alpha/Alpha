"use client";

import { Edges } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
  isActive?: boolean;
  onSelect?: () => void;
}

export function StageDiscovery({ run, isActive = false, onSelect }: Props) {
  const scanBarRef = useRef<THREE.Mesh | null>(null);
  const stage = run.stages.find((s) => s.stage === "discovery");
  const isDone = stage?.state === "succeeded";

  useFrame(({ clock }) => {
    if (scanBarRef.current) {
      scanBarRef.current.position.y = 0.65 + Math.sin(clock.getElapsedTime() * 1.8) * 0.55;
    }
  });

  return (
    <group
      position={[-10.5, 0, 0]}
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
      {/* 선택 강조 바닥 프레임 */}
      {isActive && (
        <mesh position={[0, -0.4, 0]}>
          <boxGeometry args={[3.04, 0.06, 2.24]} />
          <meshBasicMaterial color="#3730a3" />
        </mesh>
      )}

      {/* 베이스 플랫폼 */}
      <mesh position={[0, -0.35, 0]} receiveShadow>
        <boxGeometry args={[2.8, 0.14, 2.0]} />
        <meshStandardMaterial color={isActive ? "#f5f5f4" : "#e7e5e0"} roughness={0.8} />
        <Edges color={isActive ? "#3730a3" : "#1c1917"} threshold={15} />
      </mesh>

      {/* AWS ECS · RDS 아키텍처 블록 3열 */}
      {[-0.75, 0, 0.75].map((x, idx) => (
        <group key={idx} position={[x, 0.45, 0]}>
          <mesh castShadow receiveShadow>
            <boxGeometry args={[0.52, 1.45, 0.65]} />
            <meshStandardMaterial color="#ffffff" roughness={0.6} />
            <Edges color="#1c1917" threshold={15} />
          </mesh>
          {/* 상태 인디케이터 캡 */}
          <mesh position={[0, 0.76, 0]}>
            <boxGeometry args={[0.42, 0.06, 0.52]} />
            <meshStandardMaterial color={isDone ? "#047857" : "#3730a3"} />
          </mesh>
        </group>
      ))}

      {/* 스캐닝 프레임 바 */}
      <mesh ref={scanBarRef} position={[0, 0.65, 0]}>
        <boxGeometry args={[2.4, 0.03, 0.85]} />
        <meshStandardMaterial color="#3730a3" transparent opacity={0.45} />
        <Edges color="#3730a3" />
      </mesh>
    </group>
  );
}
