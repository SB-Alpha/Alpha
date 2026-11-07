"use client";

import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";

// DynamicCanvas 가 조회하는 보안 모듈의 고정 노드 id. 이 run 의 원본 노드 값이 아님을 화면에 밝힙니다.

interface Props {
  position: [number, number, number];
  isCompromised: boolean;
  isQuarantined: boolean;
  /** 보안 API 가 돌려준 값만 받습니다. 모르면 null 이고 "미제공"으로 보입니다. */
  riskScore: number | null;
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

      {/* 3D HTML 툴팁 오버레이: 보안 API 영향 범위 결과 및 우회 요청 */}
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
              위험도 {riskScore !== null ? `${(riskScore * 100).toFixed(0)}%` : "미제공"}
            </span>
          </div>

          <div className="text-center font-sans text-[10px] text-ink/75">
            {isQuarantined ? (
              <span className="text-emerald-700 font-medium">
                격리·우회를 Worker 에 반영했습니다
              </span>
            ) : (
              <span>클라우드 보안 경보가 이 run 원본 자원에 맞았습니다 (보안 패널에서 경보 확인)</span>
            )}
          </div>

          {!isQuarantined && (
            <button
              type="button"
              onClick={onReroute}
              className="mt-0.5 inline-flex items-center gap-1 rounded border border-ink bg-red-600 px-2 py-0.5 font-mono text-[10px] font-bold text-white shadow-[1px_1px_0_0_var(--color-ink)] transition-transform hover:-translate-y-0.5 hover:bg-red-700 active:translate-y-0"
            >
              <span>🚨 격리·우회 요청</span>
            </button>
          )}
        </div>
      </Html>
    </group>
  );
}
