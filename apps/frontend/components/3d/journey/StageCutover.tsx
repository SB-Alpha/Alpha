"use client";

import { Edges } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";
import { servingSide } from "@/lib/dashboard";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
  activeIdx?: number | "overview";
  onSelectStage?: (stageIdx: number) => void;
}

export function StageCutover({ run, activeIdx, onSelectStage }: Props) {
  const serving = servingSide(run);
  const isTargetServing = serving === "target";

  const appDeployStage = run.stages.find((s) => s.stage === "app_deploy");
  const isAppDeployed = appDeployStage?.state === "succeeded";

  const ringRef = useRef<THREE.Mesh | null>(null);

  useFrame((_, delta) => {
    if (ringRef.current) {
      ringRef.current.rotation.z += delta * 0.7;
    }
  });

  const isStage4Active = activeIdx === 4;
  const isStage5Active = activeIdx === 5;

  return (
    <>
      {/* ==================== 05 APP DEPLOY (X = 6.3) ==================== */}
      <group
        position={[6.3, 0, 0]}
        onClick={(e) => {
          e.stopPropagation();
          onSelectStage?.(4);
        }}
        onPointerOver={(e) => {
          e.stopPropagation();
          document.body.style.cursor = "pointer";
        }}
        onPointerOut={() => {
          document.body.style.cursor = "auto";
        }}
      >
        {isStage4Active && (
          <mesh position={[0, -0.4, 0]}>
            <boxGeometry args={[3.04, 0.06, 2.24]} />
            <meshBasicMaterial color="#3730a3" />
          </mesh>
        )}

        <mesh position={[0, -0.35, 0]} receiveShadow>
          <boxGeometry args={[2.8, 0.14, 2.0]} />
          <meshStandardMaterial color={isStage4Active ? "#f5f5f4" : "#e7e5e0"} roughness={0.8} />
          <Edges color={isStage4Active ? "#3730a3" : "#1c1917"} threshold={15} />
        </mesh>

        {/* Cloud Run 컨테이너 클러스터 */}
        {[-0.55, 0.55].map((x, idx) => (
          <mesh key={idx} position={[x, 0.35, 0]} castShadow receiveShadow>
            <boxGeometry args={[0.72, 1.25, 0.72]} />
            <meshStandardMaterial
              color={isAppDeployed ? "#047857" : "#ffffff"}
              roughness={0.5}
            />
            <Edges color="#1c1917" threshold={15} />
          </mesh>
        ))}
      </group>

      {/* ==================== 06 CUTOVER (X = 10.5) ==================== */}
      <group
        position={[10.5, 0, 0]}
        onClick={(e) => {
          e.stopPropagation();
          onSelectStage?.(5);
        }}
        onPointerOver={(e) => {
          e.stopPropagation();
          document.body.style.cursor = "pointer";
        }}
        onPointerOut={() => {
          document.body.style.cursor = "auto";
        }}
      >
        {isStage5Active && (
          <mesh position={[0, -0.4, 0]}>
            <boxGeometry args={[3.04, 0.06, 2.24]} />
            <meshBasicMaterial color="#047857" />
          </mesh>
        )}

        <mesh position={[0, -0.35, 0]} receiveShadow>
          <boxGeometry args={[2.8, 0.14, 2.0]} />
          <meshStandardMaterial color={isStage5Active ? "#f5f5f4" : "#e7e5e0"} roughness={0.8} />
          <Edges color={isStage5Active ? "#047857" : "#1c1917"} threshold={15} />
        </mesh>

        {/* 최종 전환 완료 타워 */}
        <mesh position={[0, 0.55, 0]} castShadow receiveShadow>
          <cylinderGeometry args={[0.68, 0.82, 1.65, 6]} />
          <meshStandardMaterial
            color={isTargetServing ? "#047857" : "#ffffff"}
            roughness={0.45}
          />
          <Edges color="#1c1917" threshold={15} />
        </mesh>

        {isTargetServing && (
          <mesh ref={ringRef} position={[0, -0.25, 0]} rotation={[-Math.PI / 2, 0, 0]}>
            <ringGeometry args={[1.1, 1.2, 32]} />
            <meshBasicMaterial color="#047857" side={THREE.DoubleSide} />
          </mesh>
        )}
      </group>
    </>
  );
}
