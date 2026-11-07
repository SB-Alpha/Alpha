"use client";

import { Edges } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";
import { readPlan } from "@/lib/plan";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
  isActive?: boolean;
  onSelect?: () => void;
}

export function StagePlanning({ run, isActive = false, onSelect }: Props) {
  const coreRef = useRef<THREE.Mesh | null>(null);
  const plan = readPlan(run.outputs.planning);
  const isApproved = run.approvals.includes("plan");
  const checks = plan?.checks ?? [];

  useFrame((_, delta) => {
    if (coreRef.current) {
      coreRef.current.rotation.y += delta * 0.45;
    }
  });

  return (
    <group
      position={[-6.3, 0, 0]}
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

      {/* 중앙 계획 승인 ① 아키텍처 코어 */}
      <mesh ref={coreRef} position={[0, 0.85, 0]} castShadow>
        <octahedronGeometry args={[0.48, 0]} />
        <meshStandardMaterial
          color={isApproved ? "#047857" : "#ffffff"}
          roughness={0.5}
        />
        <Edges color="#1c1917" threshold={15} />
      </mesh>

      {/* 실제 plan.checks 항목별 기둥 (pass=emerald, fail=rose, unknown=amber) */}
      {(checks.length > 0 ? checks.slice(0, 4) : [null, null, null, null]).map((chk, idx) => {
        const angle = (idx / 4) * Math.PI * 2 + Math.PI / 4;
        const x = Math.cos(angle) * 0.9;
        const z = Math.sin(angle) * 0.65;
        const color =
          chk?.result === "pass"
            ? "#047857"
            : chk?.result === "fail"
              ? "#be123c"
              : "#b45309";
        return (
          <mesh key={idx} position={[x, 0.15, z]} castShadow receiveShadow>
            <boxGeometry args={[0.32, 0.85, 0.32]} />
            <meshStandardMaterial color={chk ? color : "#d6d3cd"} roughness={0.6} />
            <Edges color="#1c1917" threshold={15} />
          </mesh>
        );
      })}
    </group>
  );
}
