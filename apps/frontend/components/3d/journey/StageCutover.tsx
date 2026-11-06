"use client";

import { Edges, Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";
import { METRIC_KEYS, numberMetric, servingSide } from "@/lib/dashboard";
import { extractTargetNodeData } from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
}

export function StageCutover({ run }: Props) {
  const target = extractTargetNodeData(run);
  const serving = servingSide(run);
  const cutoverSec = numberMetric(run, METRIC_KEYS.cutoverSeconds);
  const isTargetServing = serving === "gcp" || run.state === "succeeded";

  const appDeployStage = run.stages.find((s) => s.stage === "app_deploy");
  const isAppDeployed = appDeployStage?.state === "succeeded";

  const ringRef = useRef<THREE.Mesh | null>(null);

  useFrame((_, delta) => {
    if (ringRef.current) {
      ringRef.current.rotation.z += delta * 0.7;
    }
  });

  return (
    <>
      {/* ==================== 05 APP DEPLOY (X = 9) ==================== */}
      <group position={[9, 0, 0]}>
        <mesh position={[0, -0.35, 0]} receiveShadow>
          <boxGeometry args={[2.8, 0.14, 2.0]} />
          <meshStandardMaterial color="#e7e5e0" roughness={0.8} />
          <Edges color="#1c1917" threshold={15} />
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

        <Html position={[0, -0.75, 0]} center distanceFactor={10} zIndexRange={[10, 0]}>
          <div className="whitespace-nowrap rounded border border-ink bg-white px-2 py-0.5 font-mono text-[10px] font-semibold text-ink shadow-[2px_2px_0_0_var(--color-ink)]">
            05 APP DEPLOY · {target.healthCheck ?? "Health Check 미제공"}
          </div>
        </Html>
      </group>

      {/* ==================== 06 CUTOVER (X = 15) ==================== */}
      <group position={[15, 0, 0]}>
        <mesh position={[0, -0.35, 0]} receiveShadow>
          <boxGeometry args={[2.8, 0.14, 2.0]} />
          <meshStandardMaterial color="#e7e5e0" roughness={0.8} />
          <Edges color="#1c1917" threshold={15} />
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

        <Html position={[0, -0.75, 0]} center distanceFactor={10} zIndexRange={[10, 0]}>
          <div className="whitespace-nowrap rounded border border-ink bg-white px-2 py-0.5 font-mono text-[10px] font-semibold text-ink shadow-[2px_2px_0_0_var(--color-ink)]">
            06 CUTOVER · {serving ? `${serving.toUpperCase()} SERVING` : "대기"}
            {cutoverSec !== null && ` (${cutoverSec.toFixed(1)}s)`}
          </div>
        </Html>
      </group>
    </>
  );
}
