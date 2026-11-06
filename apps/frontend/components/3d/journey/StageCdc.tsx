"use client";

import { Edges, Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import { extractCdcArcData } from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
}

const PACKET_COUNT = 6;

export function StageCdc({ run }: Props) {
  const cdc = extractCdcArcData(run);
  const packetsRef = useRef<(THREE.Mesh | null)[]>([]);
  const offsets = useMemo(
    () => Array.from({ length: PACKET_COUNT }, (_, i) => i / PACKET_COUNT),
    []
  );
  const progressRef = useRef<number[]>([...offsets]);

  const isActive = cdc.stageState === "running" || cdc.stageState === "succeeded" || cdc.copyPercent === 100;

  useFrame((_, delta) => {
    if (!isActive) return;
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
    <group position={[3, 0, 0]}>
      {/* 베이스 플랫폼 */}
      <mesh position={[0, -0.35, 0]} receiveShadow>
        <boxGeometry args={[2.8, 0.14, 2.0]} />
        <meshStandardMaterial color="#e7e5e0" roughness={0.8} />
        <Edges color="#1c1917" threshold={15} />
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
      {isActive &&
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

      <Html position={[0, -0.75, 0]} center distanceFactor={10} zIndexRange={[10, 0]}>
        <div className="whitespace-nowrap rounded border border-ink bg-white px-2 py-0.5 font-mono text-[10px] font-semibold text-ink shadow-[2px_2px_0_0_var(--color-ink)]">
          04 DB MIGRATION · 복사 {cdc.copyPercent !== null ? `${cdc.copyPercent}%` : "미제공"} · 지연{" "}
          {cdc.lagFormatted ?? "미제공"}
        </div>
      </Html>
    </group>
  );
}
