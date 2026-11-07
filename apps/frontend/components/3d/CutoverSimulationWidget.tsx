"use client";

import dynamic from "next/dynamic";
import { useState } from "react";
import type { RunStatus } from "@/lib/workflows";
import type { Step3Mode } from "./physics/PhysicsControlsOverlay";

const PhysicsCanvasView = dynamic(() => import("./physics/PhysicsCanvasView"), {
  ssr: false,
  loading: () => (
    <div className="flex h-80 w-full items-center justify-center bg-paper font-mono text-xs text-ink/50">
      전환 트래픽 뷰 & 주행 데모 불러오는 중…
    </div>
  ),
});

interface Props {
  run: RunStatus;
  defaultOpen?: boolean;
  initialMode?: Step3Mode;
  title?: string;
}

export function CutoverSimulationWidget({
  run,
  defaultOpen = true,
  initialMode = "cutover_valve",
  title = "전환 트래픽: 실시간 Worker 상태 · 실측 기록 재생 & 파이프라인 주행",
}: Props) {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const [isFullscreen, setIsFullscreen] = useState(false);

  const containerClass = isFullscreen
    ? "fixed inset-0 z-50 flex flex-col bg-paper p-4 sm:p-6"
    : "mt-4 overflow-hidden rounded-lg border border-ink/25 bg-paper";

  return (
    <div className={containerClass}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink/15 bg-white px-3.5 py-2">
        <div className="flex items-center gap-2">
          <span className="inline-block h-2 w-2 rounded-full bg-emerald-600" />
          <h4 className="text-xs font-semibold text-ink">{title}</h4>
          <span className="hidden font-mono text-[11px] text-ink/50 sm:inline">
            (게이트 상태는 Worker 보고값, 재생은 이 run 의 cutover_steps 경과 시간)
          </span>
        </div>

        <div className="flex items-center gap-1.5">
          {isOpen && (
            <button
              type="button"
              onClick={() => setIsFullscreen((prev) => !prev)}
              className="rounded border border-ink/20 bg-white px-2 py-0.5 text-[11px] font-medium text-ink/75 hover:border-ink hover:text-ink"
            >
              {isFullscreen ? "전체화면 닫기 ✕" : "전체화면 ⛶"}
            </button>
          )}
          {!isFullscreen && (
            <button
              type="button"
              onClick={() => setIsOpen((prev) => !prev)}
              className="rounded border border-ink/20 bg-white px-2 py-0.5 text-[11px] font-medium text-ink/70 hover:border-ink hover:text-ink"
            >
              {isOpen ? "뷰 접기" : "뷰 열기"}
            </button>
          )}
        </div>
      </div>

      {isOpen && (
        <div className={isFullscreen ? "flex-1 min-h-[480px] w-full" : "h-80 sm:h-96 w-full"}>
          <PhysicsCanvasView run={run} initialMode={initialMode} />
        </div>
      )}
    </div>
  );
}
