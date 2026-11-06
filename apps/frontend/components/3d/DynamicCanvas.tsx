"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useRef, useState } from "react";
import { extractCdcArcData, extractSourceNodeData, extractTargetNodeData } from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";
import type { OrbitControlsHandle } from "./CloudMorphScene";

interface Props {
  run: RunStatus;
  onNavigateSection?: (sectionId: string) => void;
}

const CanvasView = dynamic(() => import("./CanvasView"), {
  ssr: false,
  loading: () => (
    <div className="flex h-56 w-full items-center justify-center bg-paper font-mono text-xs text-ink/50">
      토폴로지 뷰포트 불러오는 중…
    </div>
  ),
});

export function DynamicCanvas({ run, onNavigateSection }: Props) {
  const [isCollapsed, setIsCollapsed] = useState(false);
  const [selectedNode, setSelectedNode] = useState<"source" | "cdc" | "target" | null>(null);
  const controlsRef = useRef<OrbitControlsHandle | null>(null);

  const source = extractSourceNodeData(run);
  const cdc = extractCdcArcData(run);
  const target = extractTargetNodeData(run);

  const handleResetView = () => {
    setSelectedNode(null);
    controlsRef.current?.reset();
  };

  return (
    <section
      aria-label="3D 인프라 토폴로지 및 CDC 파이프라인"
      className="overflow-hidden rounded-lg border border-ink bg-white shadow-[3px_3px_0_0_var(--color-ink)]"
    >
      {/* 상단 컨트롤 바 — 운영 콘솔 종이·잉크 톤 */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink/15 bg-white px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="font-semibold text-ink">토폴로지 & CDC 경로</span>
          <span className="text-ink/25">·</span>
          <span className="font-mono text-ink/65">
            {source.region} → {target.providerSummary}
          </span>
          {cdc.tables.length > 0 && (
            <span className="rounded border border-ink/15 bg-paper px-1.5 py-0.5 font-mono text-[11px] text-ink/70">
              테이블 {cdc.tables.length}개 ({cdc.tables.filter((t) => t.match).length}/{cdc.tables.length} 일치)
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {!isCollapsed && (
            <>
              <div className="hidden items-center gap-1 sm:flex">
                <button
                  type="button"
                  onClick={handleResetView}
                  className={`rounded border px-2 py-0.5 font-mono text-[11px] transition-all active:scale-[0.96] ${
                    selectedNode === null
                      ? "border-ink bg-ink text-paper shadow-xs"
                      : "border-ink/20 bg-paper text-ink/75 hover:border-ink hover:text-ink"
                  }`}
                >
                  🌐 전체 조감도
                </button>
                {(
                  [
                    { key: "source", label: "☁️ 원본 AWS" },
                    { key: "cdc", label: "⚡ CDC 아치" },
                    { key: "target", label: "🚀 목적지 GCP" },
                  ] as const
                ).map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    onClick={() => setSelectedNode((prev) => (prev === item.key ? null : item.key))}
                    className={`rounded border px-2 py-0.5 font-mono text-[11px] transition-all active:scale-[0.96] ${
                      selectedNode === item.key
                        ? "border-ink bg-ink text-paper shadow-xs"
                        : "border-ink/20 bg-paper text-ink/75 hover:border-ink"
                    }`}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </>
          )}

          <Link
            href={`/runs/${encodeURIComponent(run.run_id)}/report`}
            className="rounded border border-ink bg-ink px-2.5 py-0.5 text-[11px] font-medium text-paper transition-transform hover:-translate-y-px"
          >
            시각적 요약 리포트 →
          </Link>

          <button
            type="button"
            onClick={() => setIsCollapsed((prev) => !prev)}
            className="rounded border border-ink/20 bg-white px-2 py-0.5 text-[11px] font-medium text-ink/70 hover:border-ink hover:text-ink"
          >
            {isCollapsed ? "토폴로지 펼치기" : "접기"}
          </button>
        </div>
      </div>

      {/* 컴팩트 3D 뷰포트 (Step 1) */}
      {!isCollapsed && (
        <div className="relative h-60 w-full bg-paper">
          <CanvasView
            run={run}
            controlsRef={controlsRef}
            selectedNode={selectedNode}
            onSelectNode={setSelectedNode}
            onNavigateSection={onNavigateSection}
          />

          {/* 하단 실측 데이터 요약 바 */}
          <div className="pointer-events-none absolute inset-x-3 bottom-2 flex flex-wrap items-center justify-between gap-2 text-[11px]">
            <span className="flex items-center gap-1.5 rounded border border-ink/15 bg-white/90 px-2 py-0.5 font-mono text-ink/70 backdrop-blur-xs shadow-xs">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-600 animate-pulse" />
              노드 클릭 시 카메라 자동 포커스(Bounds) 및 상세 실측 카드 연동
            </span>
            <div className="flex items-center gap-2 font-mono">
              <span className="rounded border border-ink/15 bg-white/90 px-2 py-0.5 text-ink tabular-nums shadow-xs">
                복사율 <strong className="font-semibold text-emerald-700">{cdc.copyPercent !== null ? `${cdc.copyPercent}%` : "미제공"}</strong>
              </span>
              <span className="rounded border border-ink/15 bg-white/90 px-2 py-0.5 text-ink tabular-nums shadow-xs">
                복제 지연 <strong className="font-semibold text-indigo-900">{cdc.lagFormatted ?? "미제공"}</strong>
              </span>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
