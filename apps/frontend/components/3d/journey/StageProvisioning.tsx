"use client";

import { Edges, Instance, Instances } from "@react-three/drei";
import { useMemo } from "react";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
  isActive?: boolean;
  onSelect?: () => void;
}

const GRID = 3; // 3x3 = 9개 Terraform 리소스 모듈 인스턴스

export function StageProvisioning({ run, isActive = false, onSelect }: Props) {
  const stage = run.stages.find((s) => s.stage === "provisioning");
  const isDone = stage?.state === "succeeded";
  const isFailed = stage?.state === "failed";

  const blocks = useMemo(() => {
    const items: { x: number; z: number; h: number }[] = [];
    const spacing = 0.65;
    const offset = ((GRID - 1) * spacing) / 2;
    for (let r = 0; r < GRID; r++) {
      for (let c = 0; c < GRID; c++) {
        const isCenter = r === 1 && c === 1;
        items.push({
          x: c * spacing - offset,
          z: r * spacing - offset,
          h: isCenter ? 1.4 : 0.75 + ((r + c) % 2) * 0.25,
        });
      }
    }
    return items;
  }, []);

  const blockColor = isFailed ? "#be123c" : isDone ? "#047857" : "#ffffff";

  return (
    <group
      position={[-2.1, 0, 0]}
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

      {/* 단 1회의 Draw Call 로 Terraform 모듈 클러스터 렌더링 */}
      <Instances limit={16} range={blocks.length} castShadow receiveShadow>
        <boxGeometry args={[0.42, 1.0, 0.42]} />
        <meshStandardMaterial color={blockColor} roughness={0.55} />
        {blocks.map((b, i) => (
          <Instance
            key={i}
            position={[b.x, -0.28 + b.h * 0.5, b.z]}
            scale={[1, isDone ? b.h : b.h * 0.5, 1]}
          />
        ))}
      </Instances>
    </group>
  );
}
