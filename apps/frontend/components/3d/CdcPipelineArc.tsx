"use client";

import { Edges, Html, Line } from "@react-three/drei";
import { useFrame, type ThreeEvent } from "@react-three/fiber";
import { useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { MAX_SAFE_LAG_BYTES } from "@/lib/dashboard";
import { extractCdcArcData } from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
  selectedNode: "source" | "cdc" | "target" | null;
  onSelectNode: (node: "source" | "cdc" | "target" | null) => void;
  onNavigateSection?: (sectionId: string) => void;
  groupRef?: React.RefObject<THREE.Group | null>;
}

const PARTICLE_COUNT = 8;

export function CdcPipelineArc({
  run,
  selectedNode,
  onSelectNode,
  onNavigateSection,
  groupRef,
}: Props) {
  const cdc = extractCdcArcData(run);
  const [hovered, setHovered] = useState(false);

  const isFailed = cdc.stageState === "failed" || (cdc.mismatchCount !== null && cdc.mismatchCount > 0);
  const isSucceeded = cdc.stageState === "succeeded" || cdc.copyPercent === 100;
  const isRunning = cdc.stageState === "running" || (cdc.copyPercent !== null && cdc.copyPercent > 0 && cdc.copyPercent < 100);
  const isActive = isRunning || isSucceeded;

  // 상태 색상 (운영 콘솔 규칙: 정상=emerald, 진행=indigo, 경고=amber, 실패=rose, 대기=ink/30)
  const strokeColor = useMemo(() => {
    if (isFailed) return "#be123c"; // rose-700
    // 전환 승인 ② 의 지연 기준(workflows/readiness.py MAX_REPLICATION_LAG_BYTES = 1 MiB)과 같은 값입니다.
    if (cdc.lagBytes !== null && cdc.lagBytes > MAX_SAFE_LAG_BYTES) return "#b45309"; // amber-700
    if (isSucceeded) return "#047857"; // emerald-700
    if (isRunning) return "#3730a3"; // indigo-800 (--color-accent)
    return "#a8a29e"; // stone-400 (pending)
  }, [isFailed, isSucceeded, isRunning, cdc.lagBytes]);

  // 3차 베지에 곡선 (Project 2 아치 수식)
  const { curve, points, tubeGeometry } = useMemo(() => {
    const p0 = new THREE.Vector3(-3.1, 0.45, 0);
    const p1 = new THREE.Vector3(0, 2.15, 0);
    const p2 = new THREE.Vector3(3.1, 0.45, 0);
    const c = new THREE.QuadraticBezierCurve3(p0, p1, p2);
    const pts = c.getPoints(48);
    const tube = new THREE.TubeGeometry(c, 48, 0.022, 8, false);
    return { curve: c, points: pts, tubeGeometry: tube };
  }, []);

  const particlesRef = useRef<(THREE.Mesh | null)[]>([]);
  const offsets = useMemo(
    () => Array.from({ length: PARTICLE_COUNT }, (_, i) => i / PARTICLE_COUNT),
    []
  );
  const progressRef = useRef<number[]>([...offsets]);

  useFrame((_, delta) => {
    if (!isActive) return;

    // 실제 copyPercent 및 stageState 에 비례하는 속도
    const baseSpeed = isRunning ? 0.38 : 0.16;
    const ratio = cdc.copyPercent !== null ? Math.max(0.2, cdc.copyPercent / 100) : 0.5;
    const speed = baseSpeed * (0.5 + ratio * 0.5);

    progressRef.current.forEach((t, i) => {
      const nextT = (t + delta * speed) % 1.0;
      progressRef.current[i] = nextT;
      const mesh = particlesRef.current[i];
      if (mesh) {
        const pt = curve.getPointAt(nextT);
        mesh.position.copy(pt);
        const pulse = 0.85 + Math.sin(nextT * Math.PI) * 0.35;
        mesh.scale.setScalar(pulse);
      }
    });
  });

  const handleRelayClick = (e: ThreeEvent<MouseEvent>) => {
    e.stopPropagation();
    onSelectNode(selectedNode === "cdc" ? null : "cdc");
  };

  const showTooltip = hovered || selectedNode === "cdc";

  return (
    <group ref={groupRef}>
      {/* 1. 베지에 곡선 가이드 아치 */}
      <mesh geometry={tubeGeometry}>
        <meshStandardMaterial
          color={strokeColor}
          transparent
          opacity={isActive ? 0.25 : 0.15}
          roughness={0.6}
        />
      </mesh>
      <Line
        points={points}
        color={strokeColor}
        lineWidth={isActive ? 2 : 1.2}
        dashed={!isActive}
        dashSize={0.18}
        gapSize={0.12}
      />

      {/* 2. 실제 복제 데이터가 있을 때만 흐르는 데이터 패킷 구체 */}
      {isActive &&
        offsets.map((_, i) => (
          <mesh
            key={i}
            ref={(el) => {
              particlesRef.current[i] = el;
            }}
          >
            <boxGeometry args={[0.11, 0.11, 0.11]} />
            <meshStandardMaterial color={strokeColor} roughness={0.3} metalness={0.1} />
            <Edges color="#1c1917" threshold={15} />
          </mesh>
        ))}

      {/* 3. 아치 정점 Debezium · Kafka 중계 노드 (클릭 시 Bounds 포커스 & 테이블 대조 툴팁) */}
      <group
        position={[0, 1.32, 0]}
        onClick={handleRelayClick}
        onPointerOver={(e) => {
          e.stopPropagation();
          document.body.style.cursor = "pointer";
          setHovered(true);
        }}
        onPointerOut={() => {
          document.body.style.cursor = "auto";
          setHovered(false);
        }}
      >
        <mesh castShadow receiveShadow>
          <octahedronGeometry args={[0.24, 0]} />
          <meshStandardMaterial
            color={selectedNode === "cdc" ? "#1c1917" : "#ffffff"}
            roughness={0.4}
            metalness={0.1}
          />
          <Edges color={strokeColor} lineWidth={1.5} threshold={15} />
        </mesh>

        {/* 상시 소형 라벨 */}
        {/* 상시 아치 중앙 라벨 */}
        <Html position={[0, -0.42, 0]} center zIndexRange={[20, 0]}>
          <button
            type="button"
            onClick={() => onSelectNode(selectedNode === "cdc" ? null : "cdc")}
            className="pointer-events-auto whitespace-nowrap rounded border border-ink/25 bg-white px-2 py-0.5 font-mono text-[10px] font-medium text-ink shadow-[1px_1px_0_0_var(--color-ink)] transition-transform hover:-translate-y-0.5"
          >
            CDC · {cdc.copyPercent !== null ? `${cdc.copyPercent}%` : "미제공"}
            {cdc.lagFormatted !== null && ` · 지연 ${cdc.lagFormatted}`}
          </button>
        </Html>

        {/* 호버/선택 시 실제 테이블 대조 및 CDC 상태 컴팩트 실측 카드 */}
        {showTooltip && (
          <Html position={[0, 0.52, 0]} center zIndexRange={[100, 50]}>
            <div
              onMouseEnter={() => setHovered(true)}
              onMouseLeave={() => setHovered(false)}
              className="pointer-events-auto w-56 rounded-lg border border-ink bg-white/98 p-2 text-left text-ink shadow-[2px_2px_0_0_var(--color-ink)] backdrop-blur-xs"
            >
              <div className="flex items-center justify-between border-b border-ink/15 pb-1">
                <span className="font-mono text-[10px] font-semibold">Debezium → Kafka → Sink</span>
                <span
                  className={`rounded px-1 py-0.2 font-mono text-[9px] font-bold ${
                    isFailed
                      ? "bg-rose-100 text-rose-800"
                      : isSucceeded
                        ? "bg-emerald-100 text-emerald-800"
                        : isRunning
                          ? "bg-indigo-100 text-indigo-900"
                          : "bg-stone-100 text-ink/60"
                  }`}
                >
                  {cdc.stageState}
                </span>
              </div>

              <dl className="mt-1.5 grid grid-cols-2 gap-x-2 gap-y-0.5 text-[10px]">
                <div>
                  <dt className="text-ink/55">복사율</dt>
                  <dd className="font-mono font-semibold text-emerald-700">
                    {cdc.copyPercent !== null ? `${cdc.copyPercent}%` : "미제공"}
                  </dd>
                </div>
                <div>
                  <dt className="text-ink/55">복제 지연 (Lag)</dt>
                  <dd className="font-mono font-semibold truncate">{cdc.lagFormatted ?? "미제공"}</dd>
                </div>
                <div>
                  <dt className="text-ink/55">불일치 테이블</dt>
                  <dd
                    className={`font-mono font-semibold ${
                      cdc.mismatchCount === 0
                        ? "text-emerald-700"
                        : cdc.mismatchCount !== null
                          ? "text-rose-700"
                          : ""
                    }`}
                  >
                    {cdc.mismatchCount !== null ? `${cdc.mismatchCount}개` : "미제공"}
                  </dd>
                </div>
              </dl>

              {/* 실제 테이블별 행 수 대조 목록 (최대 2개 컴팩트 스크롤) */}
              {cdc.tables.length > 0 && (
                <div className="mt-1 border-t border-ink/10 pt-1">
                  <p className="text-[9px] font-semibold text-ink/50">테이블 행 수 대조</p>
                  <ul className="mt-0.5 max-h-12 overflow-y-auto space-y-0.5 font-mono text-[9px]">
                    {cdc.tables.map((t) => (
                      <li key={t.name} className="flex items-center justify-between">
                        <span className="truncate font-medium">{t.name}</span>
                        <span className={t.match ? "text-emerald-700" : "text-amber-800"}>
                          {t.sourceRows.toLocaleString("ko-KR")} → {t.targetRows.toLocaleString("ko-KR")}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {onNavigateSection && (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onNavigateSection("stage-stepper-section");
                  }}
                  className="pointer-events-auto mt-1.5 w-full rounded border border-ink/20 bg-paper py-0.5 text-center font-mono text-[10px] font-medium text-ink hover:border-ink active:bg-ink/5"
                >
                  하단 단계 상세 보기 ↓
                </button>
              )}
            </div>
          </Html>
        )}
      </group>
    </group>
  );
}
