"use client";

import { Canvas } from "@react-three/fiber";
import type { EdgeSnapshot, Topology } from "@/lib/edgeLive";
import type { RunStatus } from "@/lib/workflows";
import { CloudMorphScene, type OrbitControlsHandle } from "./CloudMorphScene";

interface Props {
  run: RunStatus;
  controlsRef?: React.MutableRefObject<OrbitControlsHandle | null>;
  selectedNode: "source" | "cdc" | "target" | null;
  onSelectNode: (node: "source" | "cdc" | "target" | null) => void;
  onNavigateSection?: (sectionId: string) => void;
  isCompromised?: boolean;
  isQuarantined?: boolean;
  riskScore?: number | null;
  onReroute?: () => void;
  showFullTopology?: boolean;
  topology?: Topology | null;
  topologyError?: string | null;
  edgeLive?: EdgeSnapshot | null;
}

export default function CanvasView({
  run,
  controlsRef,
  selectedNode,
  onSelectNode,
  onNavigateSection,
  isCompromised,
  isQuarantined,
  riskScore,
  onReroute,
  showFullTopology,
  topology,
  topologyError,
  edgeLive,
}: Props) {
  return (
    <Canvas
      shadows
      dpr={[1, 2]}
      gl={{ antialias: true }}
      className="h-full w-full select-none"
      // 감싸는 div 는 overflow:hidden 인데, 가장자리 라벨을 누르면 브라우저가 이 div 를 스크롤해
      // 장면과 상세 카드가 위로 밀려 헤더 밑에 가려집니다. 스크롤을 늘 0 으로 되돌립니다.
      onScroll={(e) => {
        e.currentTarget.scrollTop = 0;
        e.currentTarget.scrollLeft = 0;
      }}
    >
      <CloudMorphScene
        run={run}
        controlsRef={controlsRef}
        selectedNode={selectedNode}
        onSelectNode={onSelectNode}
        onNavigateSection={onNavigateSection}
        isCompromised={isCompromised}
        isQuarantined={isQuarantined}
        riskScore={riskScore}
        onReroute={onReroute}
        showFullTopology={showFullTopology}
        topology={topology}
        topologyError={topologyError}
        edgeLive={edgeLive}
      />
    </Canvas>
  );
}
