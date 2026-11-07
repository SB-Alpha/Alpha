"use client";

import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";

interface Props {
  position: [number, number, number];
  isCompromised: boolean;
  isQuarantined: boolean;
  riskScore: number;
  onReroute: () => void;
}

export function BlastRadiusMesh({
  position,
  isCompromised,
  isQuarantined,
  riskScore,
  onReroute,
}: Props) {
  const outerSphereRef = useRef<THREE.Mesh | null>(null);
  const pulseRingRef = useRef<THREE.Mesh | null>(null);

  useFrame((state) => {
    const t = state.clock.getElapsedTime();
    if (outerSphereRef.current) {
      const pulse = 1.0 + Math.sin(t * 3.5) * 0.12;
      outerSphereRef.current.scale.set(pulse, pulse, pulse);
      if (outerSphereRef.current.material instanceof THREE.MeshBasicMaterial) {
        outerSphereRef.current.material.opacity = 0.22 + Math.sin(t * 3.5) * 0.08;
      }
    }
    if (pulseRingRef.current) {
      pulseRingRef.current.rotation.z += 0.02;
    }
  });

  if (!isCompromised && !isQuarantined) return null;

  return (
    <group position={position}>
      {/* 3D 침해 영향 반경(Blast Radius) 반투명 맥동 구체 */}
      <mesh ref={outerSphereRef}>
        <sphereGeometry args={[1.3, 24, 24]} />
        <meshBasicMaterial
          color={isQuarantined ? "#f59e0b" : "#ef4444"}
          transparent
          opacity={0.25}
          wireframe
        />
      </mesh>

      {/* 내부 고밀도 위험 코어 구체 */}
      <mesh scale={[0.45, 0.45, 0.45]}>
        <sphereGeometry args={[1, 16, 16]} />
        <meshStandardMaterial
          color={isQuarantined ? "#d97706" : "#dc2626"}
          emissive={isQuarantined ? "#b45309" : "#991b1b"}
          emissiveIntensity={0.6}
          roughness={0.2}
        />
      </mesh>

      {/* 바닥 경고 펄스 링 */}
      <mesh ref={pulseRingRef} position={[0, -0.32, 0]} rotation={[-Math.PI / 2, 0, 0]}>
        <ringGeometry args={[1.15, 1.25, 32]} />
        <meshBasicMaterial
          color={isQuarantined ? "#f59e0b" : "#ef4444"}
          side={THREE.DoubleSide}
          transparent
          opacity={0.6}
        />
      </mesh>

      {/* 3D HTML 툴팁 오버레이: 실시간 GuardDuty 경고 및 즉각 우회 제어 */}
      <Html position={[0, 1.45, 0]} center zIndexRange={[120, 80]}>
        <div className="pointer-events-auto flex flex-col items-center gap-1 rounded-md border border-ink bg-white/95 p-2 shadow-[2px_2px_0_0_var(--color-ink)] backdrop-blur-xs">
          <div className="flex items-center gap-1.5 font-mono text-[10px]">
            <span
              className={`h-2 w-2 animate-ping rounded-full ${
                isQuarantined ? "bg-amber-500" : "bg-red-500"
              }`}
            />
            <span className="font-bold text-ink">
              {isQuarantined ? "ISOLATED (격리됨)" : "BLAST RADIUS DETECTED"}
            </span>
            <span
              className={`rounded px-1 text-[9px] font-bold ${
                isQuarantined
                  ? "bg-amber-100 text-amber-900"
                  : "bg-red-100 text-red-900"
              }`}
            >
              위험도 {(riskScore * 100).toFixed(0)}%
            </span>
          </div>

          <div className="text-center font-sans text-[10px] text-ink/75">
            {isQuarantined ? (
              <span className="text-emerald-700 font-medium">
                GCP Cloud Run 격리 우회 활성 (원본 쓰기 차단됨)
              </span>
            ) : (
              <span>AWS ECS 태스크 침해 탐지 (GuardDuty CVE-2026-4192 시뮬레이션)</span>
            )}
          </div>

          {!isQuarantined && (
            <button
              type="button"
              onClick={onReroute}
              className="mt-0.5 inline-flex items-center gap-1 rounded border border-ink bg-red-600 px-2 py-0.5 font-mono text-[10px] font-bold text-white shadow-[1px_1px_0_0_var(--color-ink)] transition-transform hover:-translate-y-0.5 hover:bg-red-700 active:translate-y-0"
            >
              <span>🚨 목표 0.4초 내 GCP 즉각 우회 (Drag/Click)</span>
            </button>
          )}
        </div>
      </Html>
    </group>
  );
}
