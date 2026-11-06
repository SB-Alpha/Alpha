"use client";

import { ScrollControls } from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
import { useState } from "react";
import { STAGE_LABEL } from "@/lib/labels";
import { STAGE_ORDER, type RunStatus } from "@/lib/workflows";
import { JourneyHtmlOverlay } from "./JourneyHtmlOverlay";
import { MigrationJourneyScene } from "./MigrationJourneyScene";

interface Props {
  run: RunStatus;
}

export default function JourneyCanvasView({ run }: Props) {
  const [activeIdx, setActiveIdx] = useState(0);

  return (
    <div className="relative h-full w-full overflow-hidden bg-paper select-none">
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

      {/* 우측 상단 6단계 진행 인디케이터 */}
      <div className="pointer-events-none absolute right-4 top-3 flex flex-wrap items-center gap-1 rounded-md border border-ink/20 bg-white/95 px-2.5 py-1.5 shadow-[2px_2px_0_0_var(--color-ink)]">
        {STAGE_ORDER.map((stageName, i) => {
          const isCurrent = activeIdx === i;
          return (
            <span
              key={stageName}
              className={`rounded px-1.5 py-0.5 font-mono text-[10px] transition-colors ${
                isCurrent
                  ? "bg-ink font-semibold text-paper"
                  : "text-ink/50"
              }`}
            >
              {String(i + 1).padStart(2, "0")} {STAGE_LABEL[stageName]}
            </span>
          );
        })}
      </div>

      {/* 하단 스크롤 안내 */}
      <div className="pointer-events-none absolute bottom-2.5 right-4 rounded border border-ink/15 bg-white/90 px-2.5 py-1 font-mono text-[11px] text-ink/65">
        마우스 휠 / 트랙패드 스크롤로 01~06 단계 플라이스루 탐색 ({activeIdx + 1}/6)
      </div>
    </div>
  );
}
