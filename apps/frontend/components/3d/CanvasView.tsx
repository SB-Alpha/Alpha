"use client";

import { Canvas } from "@react-three/fiber";
import type { RunStatus } from "@/lib/workflows";
import { CloudMorphScene, type OrbitControlsHandle } from "./CloudMorphScene";

interface Props {
  run: RunStatus;
  controlsRef?: React.MutableRefObject<OrbitControlsHandle | null>;
  selectedNode: "source" | "cdc" | "target" | null;
  onSelectNode: (node: "source" | "cdc" | "target" | null) => void;
  onNavigateSection?: (sectionId: string) => void;
}

export default function CanvasView({
  run,
  controlsRef,
  selectedNode,
  onSelectNode,
  onNavigateSection,
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
      />
    </Canvas>
  );
}
