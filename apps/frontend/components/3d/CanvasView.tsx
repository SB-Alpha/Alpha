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
