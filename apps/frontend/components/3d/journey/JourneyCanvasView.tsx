"use client";

import { Canvas } from "@react-three/fiber";
import { useCallback, useEffect, useState } from "react";
import { STAGE_LABEL } from "@/lib/labels";
import { STAGE_ORDER, type RunStatus } from "@/lib/workflows";
import { JourneyHtmlOverlay } from "./JourneyHtmlOverlay";
import { MigrationJourneyScene, type JourneyViewMode } from "./MigrationJourneyScene";

interface Props {
  run: RunStatus;
}

export default function JourneyCanvasView({ run }: Props) {
  const [activeStageIdx, setActiveStageIdx] = useState<number>(0);
  const [isOverview, setIsOverview] = useState<boolean>(false);
  const [flyTrigger, setFlyTrigger] = useState<number>(0);
  const [zoomScale, setZoomScale] = useState<number>(1.0);

  const selectStage = useCallback((stageIdx: number) => {
    const clamped = Math.max(0, Math.min(5, stageIdx)) as 0 | 1 | 2 | 3 | 4 | 5;
    setActiveStageIdx(clamped);
    setIsOverview(false);
    setFlyTrigger((prev) => prev + 1);
  }, []);

  const toggleOverview = useCallback(() => {
    setIsOverview((prev) => !prev);
    setZoomScale(1.0);
    setFlyTrigger((prev) => prev + 1);
  }, []);

  const resetCamera = useCallback(() => {
    setZoomScale(1.0);
    setFlyTrigger((prev) => prev + 1);
  }, []);

  // 키보드 좌/우 방향키 탐색 지원 (버튼 포커스나 Space 스크롤을 방해하지 않도록 화살표 좌우키만 처리)
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement ||
        e.target instanceof HTMLSelectElement ||
        e.target instanceof HTMLButtonElement
      ) {
        return;
      }
      if (e.key === "ArrowRight") {
        setIsOverview(false);
        setActiveStageIdx((prev) => {
          const next = Math.min(5, prev + 1);
          setFlyTrigger((t) => t + 1);
          return next;
        });
      } else if (e.key === "ArrowLeft") {
        setIsOverview(false);
        setActiveStageIdx((prev) => {
          const next = Math.max(0, prev - 1);
          setFlyTrigger((t) => t + 1);
          return next;
        });
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const activeView: JourneyViewMode = isOverview
    ? "overview"
    : (activeStageIdx as 0 | 1 | 2 | 3 | 4 | 5);

  return (
    <div className="flex h-full w-full flex-col overflow-hidden bg-paper select-none">
      {/* 1. 상단 전용 컨트롤 헤더 바 (캔버스를 가리지 않는 분리형 헤더) */}
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-ink/15 bg-white px-3 py-2">
        <div className="flex flex-wrap items-center gap-1">
          <button
            type="button"
            onClick={() => selectStage(activeStageIdx - 1)}
            disabled={!isOverview && activeStageIdx === 0}
            className="cursor-pointer rounded border border-ink/20 bg-paper px-2 py-0.5 font-mono text-xs font-semibold text-ink hover:border-ink disabled:cursor-not-allowed disabled:opacity-35"
            title="이전 단계 (←)"
          >
            ◀
          </button>

          {STAGE_ORDER.map((stageName, i) => {
            const isCurrent = !isOverview && activeStageIdx === i;
            return (
              <button
                key={stageName}
                type="button"
                onClick={() => selectStage(i)}
                className={`cursor-pointer rounded border px-2 py-0.5 font-mono text-[11px] transition-all ${
                  isCurrent
                    ? "border-ink bg-ink font-semibold text-paper shadow-xs"
                    : "border-ink/15 bg-paper text-ink/70 hover:border-ink hover:text-ink font-medium"
                }`}
              >
                {String(i + 1).padStart(2, "0")} {STAGE_LABEL[stageName]}
              </button>
            );
          })}

          <button
            type="button"
            onClick={() => selectStage(activeStageIdx + 1)}
            disabled={!isOverview && activeStageIdx === 5}
            className="cursor-pointer rounded border border-ink/20 bg-paper px-2 py-0.5 font-mono text-xs font-semibold text-ink hover:border-ink disabled:cursor-not-allowed disabled:opacity-35"
            title="다음 단계 (→)"
          >
            ▶
          </button>
        </div>

        {/* 우측 뷰포트 제어 도구 */}
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={toggleOverview}
            className={`cursor-pointer rounded border px-2 py-0.5 font-mono text-[11px] font-medium transition-all ${
              isOverview
                ? "border-ink bg-ink text-paper shadow-xs"
                : "border-ink/20 bg-paper text-ink/75 hover:border-ink hover:text-ink"
            }`}
          >
            🌐 전체 6단계 조망
          </button>
          <button
            type="button"
            onClick={() => setZoomScale((z) => Math.max(0.65, +(z - 0.15).toFixed(2)))}
            className="cursor-pointer rounded border border-ink/20 bg-paper px-1.5 py-0.5 font-mono text-[11px] text-ink/75 hover:border-ink hover:text-ink"
            title="확대"
          >
            ＋
          </button>
          <button
            type="button"
            onClick={() => setZoomScale((z) => Math.min(1.45, +(z + 0.15).toFixed(2)))}
            className="cursor-pointer rounded border border-ink/20 bg-paper px-1.5 py-0.5 font-mono text-[11px] text-ink/75 hover:border-ink hover:text-ink"
            title="축소"
          >
            －
          </button>
          <button
            type="button"
            onClick={resetCamera}
            className="cursor-pointer rounded border border-ink/20 bg-paper px-2 py-0.5 font-mono text-[11px] text-ink/75 hover:border-ink hover:text-ink"
            title="카메라 시점 초기화"
          >
            시점 리셋
          </button>
        </div>
      </div>

      {/* 2. 본문 영역: 좌측 단일 실측 패널 + 우측 순수 3D 뷰포트 (팝업 중첩 0개) */}
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <JourneyHtmlOverlay
          run={run}
          activeStageIdx={activeStageIdx}
          isOverview={isOverview}
          onSelectStage={selectStage}
        />

        <div className="relative min-h-[260px] flex-1 bg-paper">
          <Canvas
            shadows
            dpr={[1, 2]}
            gl={{ antialias: true }}
            className="h-full w-full"
          >
            <MigrationJourneyScene
              run={run}
              activeView={activeView}
              flyTrigger={flyTrigger}
              zoomScale={zoomScale}
              onSelectStage={selectStage}
            />
          </Canvas>

          {/* 하단 우측 최소 안내 배지 */}
          <div className="pointer-events-none absolute bottom-2 right-3 rounded border border-ink/15 bg-white/90 px-2.5 py-0.5 font-mono text-[10px] text-ink/65 shadow-xs">
            마우스 드래그: 3D 회전·이동 · 3D 블록 클릭 또는 ←/→ 키: 단계 이동 ({activeStageIdx + 1}/6)
          </div>
        </div>
      </div>
    </div>
  );
}
