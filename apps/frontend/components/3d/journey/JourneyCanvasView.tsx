"use client";

import { ScrollControls } from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
import { useCallback, useEffect, useRef, useState } from "react";
import { STAGE_LABEL } from "@/lib/labels";
import { STAGE_ORDER, type RunStatus } from "@/lib/workflows";
import { JourneyHtmlOverlay } from "./JourneyHtmlOverlay";
import { MigrationJourneyScene } from "./MigrationJourneyScene";

interface Props {
  run: RunStatus;
}

export default function JourneyCanvasView({ run }: Props) {
  const [activeIdx, setActiveIdx] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);

  const getScrollContainer = useCallback(() => {
    const container = containerRef.current;
    if (!container) return null;
    return container.querySelector<HTMLDivElement>(
      'div[style*="overflow-y: auto"], div[style*="overflow-y:auto"], div[style*="overflow"]'
    );
  }, []);

  const scrollToStage = useCallback(
    (stageIdx: number) => {
      const scrollEl = getScrollContainer();
      if (!scrollEl) return;
      const maxScroll = scrollEl.scrollHeight - scrollEl.clientHeight;
      const targetScroll = (maxScroll * stageIdx) / 5;
      scrollEl.scrollTo({ top: targetScroll, behavior: "smooth" });
      setActiveIdx(stageIdx);
    },
    [getScrollContainer]
  );

  // 마우스 휠 및 트랙패드 스크롤 이벤트 캡처: 외부 페이지 스크롤 차단 및 내부 3D 스크롤러 직결
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const onWheel = (e: WheelEvent) => {
      const scrollEl = getScrollContainer();
      if (!scrollEl) return;
      e.preventDefault();
      e.stopPropagation();
      scrollEl.scrollTop += e.deltaY;
    };

    container.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      container.removeEventListener("wheel", onWheel);
    };
  }, [getScrollContainer]);

  // 키보드 방향키(←, →, Space) 탐색 지원
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === "ArrowRight" || e.key === "ArrowDown" || e.key === " ") {
        e.preventDefault();
        setActiveIdx((prev) => {
          const next = Math.min(5, prev + 1);
          scrollToStage(next);
          return next;
        });
      } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIdx((prev) => {
          const next = Math.max(0, prev - 1);
          scrollToStage(next);
          return next;
        });
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [scrollToStage]);

  return (
    <div
      ref={containerRef}
      style={{ overscrollBehavior: "contain", touchAction: "none" }}
      className="relative h-full w-full overflow-hidden bg-paper select-none"
    >
      <Canvas
        shadows
        dpr={[1, 2]}
        gl={{ antialias: true }}
        className="h-full w-full"
      >
        <ScrollControls pages={6} damping={0.2}>
          <MigrationJourneyScene run={run} onActiveStageChange={setActiveIdx} />
          <JourneyHtmlOverlay run={run} />
        </ScrollControls>
      </Canvas>

      {/* 우측 상단 6단계 진행 인디케이터 + 원클릭 단계 이동 & 이전/다음 버튼 */}
      <div className="absolute right-4 top-3 z-10 flex flex-wrap items-center gap-1.5 rounded-md border border-ink/20 bg-white/95 p-1.5 shadow-[2px_2px_0_0_var(--color-ink)]">
        <button
          type="button"
          onClick={() => scrollToStage(Math.max(0, activeIdx - 1))}
          disabled={activeIdx === 0}
          className="cursor-pointer rounded px-2 py-0.5 text-xs font-semibold text-ink/70 hover:bg-ink/10 disabled:cursor-not-allowed disabled:opacity-30"
          title="이전 단계 (←)"
        >
          ◀
        </button>
        {STAGE_ORDER.map((stageName, i) => {
          const isCurrent = activeIdx === i;
          return (
            <button
              key={stageName}
              type="button"
              onClick={() => scrollToStage(i)}
              className={`cursor-pointer rounded px-2 py-0.5 font-mono text-[11px] transition-all ${
                isCurrent
                  ? "bg-ink font-semibold text-paper shadow-sm scale-105"
                  : "text-ink/60 hover:bg-ink/10 hover:text-ink font-medium"
              }`}
            >
              {String(i + 1).padStart(2, "0")} {STAGE_LABEL[stageName]}
            </button>
          );
        })}
        <button
          type="button"
          onClick={() => scrollToStage(Math.min(5, activeIdx + 1))}
          disabled={activeIdx === 5}
          className="cursor-pointer rounded px-2 py-0.5 text-xs font-semibold text-ink/70 hover:bg-ink/10 disabled:cursor-not-allowed disabled:opacity-30"
          title="다음 단계 (→ / Space)"
        >
          ▶
        </button>
      </div>

      {/* 하단 스크롤 & 키보드 & 단계 안내 */}
      <div className="absolute bottom-2.5 right-4 z-10 flex items-center gap-2 rounded border border-ink/15 bg-white/95 px-3 py-1 font-mono text-[11px] text-ink/75 shadow-sm">
        <span>휠 · 트랙패드 · 방향키(←/→) · 단계 버튼 클릭</span>
        <span className="font-semibold text-ink">({activeIdx + 1}/6)</span>
      </div>
    </div>
  );
}
