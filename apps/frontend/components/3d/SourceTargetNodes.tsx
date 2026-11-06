"use client";

import { Edges, Html, useBounds } from "@react-three/drei";
import { useFrame, type ThreeEvent } from "@react-three/fiber";
import { useRef, useState } from "react";
import * as THREE from "three";
import { extractSourceNodeData, extractTargetNodeData } from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
  selectedNode: "source" | "cdc" | "target" | null;
  onSelectNode: (node: "source" | "cdc" | "target" | null) => void;
  onNavigateSection?: (sectionId: string) => void;
}

export function SourceTargetNodes({ run, selectedNode, onSelectNode, onNavigateSection }: Props) {
  const source = extractSourceNodeData(run);
  const target = extractTargetNodeData(run);
  const bounds = useBounds();

  const [hoveredNode, setHoveredNode] = useState<"source" | "target" | null>(null);

  const sourceRingRef = useRef<THREE.Mesh | null>(null);
  const targetRingRef = useRef<THREE.Mesh | null>(null);

  useFrame((_, delta) => {
    if (sourceRingRef.current) {
      sourceRingRef.current.rotation.z += delta * 0.6;
    }
    if (targetRingRef.current) {
      targetRingRef.current.rotation.z += delta * 0.6;
    }
  });

  const handleClickNode = (e: ThreeEvent<MouseEvent>, node: "source" | "target") => {
    e.stopPropagation();
    const next = selectedNode === node ? null : node;
    onSelectNode(next);
    if (next) {
      bounds.refresh(e.object).fit();
    } else {
      bounds.refresh().fit();
    }
  };

  const showSourceTooltip = hoveredNode === "source" || selectedNode === "source";
  const showTargetTooltip = hoveredNode === "target" || selectedNode === "target";

  return (
    <group>
      {/* ==================== 1. SOURCE NODE (AWS ECS · RDS) ==================== */}
      <group
        position={[-3.1, 0, 0]}
        onClick={(e) => handleClickNode(e, "source")}
        onPointerOver={(e) => {
          e.stopPropagation();
          document.body.style.cursor = "pointer";
          setHoveredNode("source");
        }}
        onPointerOut={() => {
          document.body.style.cursor = "auto";
          setHoveredNode(null);
        }}
      >
        {/* 베이스 받침판 */}
        <mesh position={[0, -0.35, 0]} receiveShadow>
          <boxGeometry args={[1.65, 0.12, 1.35]} />
          <meshStandardMaterial color="#e7e5e0" roughness={0.8} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>

        {/* 좌측 ECS 컨테이너 블록 */}
        <mesh position={[-0.36, 0.25, 0]} castShadow receiveShadow>
          <boxGeometry args={[0.62, 1.05, 0.72]} />
          <meshStandardMaterial
            color={selectedNode === "source" ? "#f5f5f4" : "#ffffff"}
            roughness={0.6}
          />
          <Edges color="#1c1917" threshold={15} />
        </mesh>

        {/* 우측 RDS 데이터베이스 실린더 */}
        <mesh position={[0.38, 0.2, 0]} castShadow receiveShadow>
          <cylinderGeometry args={[0.32, 0.32, 0.92, 20]} />
          <meshStandardMaterial
            color={source.isServing ? "#3730a3" : "#d6d3cd"}
            roughness={0.5}
          />
          <Edges color="#1c1917" threshold={15} />
        </mesh>

        {/* 현재 트래픽 처리(serving === "aws") 인디케이터 링 */}
        {source.isServing && (
          <mesh ref={sourceRingRef} position={[0, -0.26, 0]} rotation={[-Math.PI / 2, 0, 0]}>
            <ringGeometry args={[1.05, 1.13, 32]} />
            <meshBasicMaterial color="#3730a3" side={THREE.DoubleSide} />
          </mesh>
        )}

        {/* 상시 하단 라벨 */}
        <Html position={[0, -0.72, 0]} center distanceFactor={9} zIndexRange={[20, 0]}>
          <button
            type="button"
            onClick={() => onSelectNode(selectedNode === "source" ? null : "source")}
            className="pointer-events-auto whitespace-nowrap rounded border border-ink bg-white px-2.5 py-0.5 font-mono text-[11px] font-semibold text-ink shadow-[2px_2px_0_0_var(--color-ink)] transition-transform hover:-translate-y-0.5"
          >
            원본 AWS ({source.region})
            {source.isServing && (
              <span className="ml-1.5 rounded bg-indigo-100 px-1 py-0.2 text-[9px] font-bold text-indigo-900">
                SERVING
              </span>
            )}
          </button>
        </Html>

        {/* 호버/클릭 상세 툴팁 (실제 planning.source / request 값 연동) */}
        {showSourceTooltip && (
          <Html position={[0, 1.38, 0]} center distanceFactor={8.5} zIndexRange={[100, 50]}>
            <div className="w-60 rounded-lg border border-ink bg-white p-3 text-left text-ink shadow-[3px_3px_0_0_var(--color-ink)]">
              <div className="flex items-center justify-between border-b border-ink/15 pb-1.5">
                <span className="text-xs font-semibold">원본 인프라 (AWS)</span>
                <span
                  className={`rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold ${
                    source.isServing ? "bg-indigo-100 text-indigo-900" : "bg-stone-100 text-ink/60"
                  }`}
                >
                  {source.isServing ? "SERVING" : "STANDBY"}
                </span>
              </div>

              <dl className="mt-2 space-y-1 text-xs">
                <div className="flex justify-between gap-2">
                  <dt className="text-ink/60">리전</dt>
                  <dd className="font-mono font-medium">{source.region}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-ink/60">ECS 서비스</dt>
                  <dd className="truncate font-mono">{source.ecsService ?? "미제공"}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-ink/60">RDS 인스턴스</dt>
                  <dd className="truncate font-mono">{source.rdsInstanceId ?? "미제공"}</dd>
                </div>
                <div className="flex justify-between gap-2 border-t border-ink/10 pt-1">
                  <dt className="text-ink/60">원본 행 수</dt>
                  <dd className="font-mono font-semibold">
                    {source.sourceRows !== null ? `${source.sourceRows.toLocaleString("ko-KR")}행` : "미제공"}
                  </dd>
                </div>
              </dl>

              {onNavigateSection && (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onNavigateSection("plan-heading");
                  }}
                  className="pointer-events-auto mt-2.5 w-full rounded border border-ink/25 bg-paper px-2 py-1 text-center text-[11px] font-medium text-ink hover:border-ink"
                >
                  승인 ① 계획 상세로 이동 ↓
                </button>
              )}
            </div>
          </Html>
        )}
      </group>

      {/* ==================== 2. TARGET NODE (GCP Cloud Run · Cloud SQL) ==================== */}
      <group
        position={[3.1, 0, 0]}
        onClick={(e) => handleClickNode(e, "target")}
        onPointerOver={(e) => {
          e.stopPropagation();
          document.body.style.cursor = "pointer";
          setHoveredNode("target");
        }}
        onPointerOut={() => {
          document.body.style.cursor = "auto";
          setHoveredNode(null);
        }}
      >
        {/* 베이스 받침판 */}
        <mesh position={[0, -0.35, 0]} receiveShadow>
          <boxGeometry args={[1.65, 0.12, 1.35]} />
          <meshStandardMaterial color="#e7e5e0" roughness={0.8} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>

        {/* 좌측 Cloud Run 컨테이너 블록 */}
        <mesh position={[-0.36, 0.28, 0]} castShadow receiveShadow>
          <boxGeometry args={[0.62, 1.12, 0.72]} />
          <meshStandardMaterial
            color={target.isServing ? "#047857" : "#ffffff"}
            roughness={0.5}
          />
          <Edges color="#1c1917" threshold={15} />
        </mesh>

        {/* 우측 Cloud SQL PostgreSQL 실린더 */}
        <mesh position={[0.38, 0.22, 0]} castShadow receiveShadow>
          <cylinderGeometry args={[0.32, 0.32, 0.96, 20]} />
          <meshStandardMaterial
            color={target.dbInstanceId ? "#ffffff" : "#e7e5e0"}
            roughness={0.5}
          />
          <Edges color="#1c1917" threshold={15} />
        </mesh>

        {/* 현재 트래픽 처리(serving === "gcp") 인디케이터 링 */}
        {target.isServing && (
          <mesh ref={targetRingRef} position={[0, -0.26, 0]} rotation={[-Math.PI / 2, 0, 0]}>
            <ringGeometry args={[1.05, 1.14, 32]} />
            <meshBasicMaterial color="#047857" side={THREE.DoubleSide} />
          </mesh>
        )}

        {/* 상시 하단 라벨 */}
        <Html position={[0, -0.72, 0]} center distanceFactor={9} zIndexRange={[20, 0]}>
          <button
            type="button"
            onClick={() => onSelectNode(selectedNode === "target" ? null : "target")}
            className="pointer-events-auto whitespace-nowrap rounded border border-ink bg-white px-2.5 py-0.5 font-mono text-[11px] font-semibold text-ink shadow-[2px_2px_0_0_var(--color-ink)] transition-transform hover:-translate-y-0.5"
          >
            목적지 {target.providerSummary}
            {target.isServing && (
              <span className="ml-1.5 rounded bg-emerald-100 px-1 py-0.2 text-[9px] font-bold text-emerald-800">
                SERVING
              </span>
            )}
          </button>
        </Html>

        {/* 호버/클릭 상세 툴팁 (실제 outputs.provisioning & app_deploy 연동) */}
        {showTargetTooltip && (
          <Html position={[0, 1.42, 0]} center distanceFactor={8.5} zIndexRange={[100, 50]}>
            <div className="w-64 rounded-lg border border-ink bg-white p-3 text-left text-ink shadow-[3px_3px_0_0_var(--color-ink)]">
              <div className="flex items-center justify-between border-b border-ink/15 pb-1.5">
                <span className="text-xs font-semibold">목적지 인프라 ({target.providerSummary})</span>
                <span
                  className={`rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold ${
                    target.isServing ? "bg-emerald-100 text-emerald-800" : "bg-stone-100 text-ink/60"
                  }`}
                >
                  {target.isServing ? "SERVING" : "PROVISIONED"}
                </span>
              </div>

              <dl className="mt-2 space-y-1 text-xs">
                <div className="flex justify-between gap-2">
                  <dt className="text-ink/60">Cloud SQL</dt>
                  <dd className="truncate font-mono">{target.dbInstanceId ?? "미제공"}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-ink/60">사설 IP</dt>
                  <dd className="font-mono">{target.cloudSqlIp ?? "미제공"}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-ink/60">사양</dt>
                  <dd className="font-mono">{target.cpuMemory ?? "미제공"}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-ink/60">상태 확인</dt>
                  <dd className="font-mono text-emerald-700">{target.healthCheck ?? "미제공"}</dd>
                </div>
                <div className="flex justify-between gap-2 border-t border-ink/10 pt-1">
                  <dt className="text-ink/60">목적지 행 수</dt>
                  <dd className="font-mono font-semibold">
                    {target.targetRows !== null ? `${target.targetRows.toLocaleString("ko-KR")}행` : "미제공"}
                  </dd>
                </div>
              </dl>

              {onNavigateSection && (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onNavigateSection("cutover-section");
                  }}
                  className="pointer-events-auto mt-2.5 w-full rounded border border-ink/25 bg-paper px-2 py-1 text-center text-[11px] font-medium text-ink hover:border-ink"
                >
                  전환 승인 / 기록으로 이동 ↓
                </button>
              )}
            </div>
          </Html>
        )}
      </group>
    </group>
  );
}
