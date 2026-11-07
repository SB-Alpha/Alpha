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

  // 트랙패드 및 휠 스크롤 비활성화: 오작동 방지를 위해 휠/트랙패드 입력을 완전히 차단
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const blockWheel = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
    };

    container.addEventListener("wheel", blockWheel, { passive: false });
    return () => {
      container.removeEventListener("wheel", blockWheel);
    };
  }, []);

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

      {/* 하단 키보드 & 단계 안내 */}
      <div className="absolute bottom-2.5 right-4 z-10 flex items-center gap-2 rounded border border-ink/15 bg-white/95 px-3 py-1 font-mono text-[11px] text-ink/75 shadow-sm">
        <span>상단 단계 버튼(01~06) 클릭 또는 방향키(←/→/Space)로 이동</span>
        <span className="font-semibold text-ink">({activeIdx + 1}/6)</span>
      </div>
    </div>
  );
}
