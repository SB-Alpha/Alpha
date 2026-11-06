"use client";

import { Bounds, ContactShadows, OrbitControls, PerspectiveCamera } from "@react-three/drei";
import { useRef, type ComponentRef } from "react";
import type { RunStatus } from "@/lib/workflows";
import { CdcPipelineArc } from "./CdcPipelineArc";
import { SourceTargetNodes } from "./SourceTargetNodes";

export type OrbitControlsHandle = ComponentRef<typeof OrbitControls>;

interface Props {
  run: RunStatus;
  controlsRef?: React.MutableRefObject<OrbitControlsHandle | null>;
  selectedNode: "source" | "cdc" | "target" | null;
  onSelectNode: (node: "source" | "cdc" | "target" | null) => void;
  onNavigateSection?: (sectionId: string) => void;
}

export function CloudMorphScene({
  run,
  controlsRef,
  selectedNode,
  onSelectNode,
  onNavigateSection,
}: Props) {
  const localControlsRef = useRef<OrbitControlsHandle | null>(null);

  const setControls = (controls: OrbitControlsHandle | null) => {
    localControlsRef.current = controls;
    if (controlsRef) {
      controlsRef.current = controls;
    }
  };

  return (
    <>
      <color attach="background" args={["#f6f4ef"]} />

      {/* 1. 아이소메트릭 느낌의 원근 카메라 */}
      <PerspectiveCamera makeDefault position={[0, 2.3, 6.9]} fov={38} />

      {/* 2. 궤도 컨트롤 */}
      <OrbitControls
        ref={setControls}
        makeDefault
        enablePan={false}
        enableZoom={true}
        minDistance={3.5}
        maxDistance={10}
        minPolarAngle={Math.PI / 6}
        maxPolarAngle={Math.PI / 2 - 0.08}
        dampingFactor={0.08}
      />

      {/* 3. 건축 모형 스튜디오 조명 (밝은 종이·잉크 톤) */}
      <ambientLight intensity={1.15} />
      <directionalLight position={[6, 10, 6]} intensity={1.1} castShadow />
      <directionalLight position={[-6, 5, -4]} intensity={0.45} />

      {/* 4. 바닥 그리드 & 소프트 그림자 */}
      <group position={[0, -0.42, 0]}>
        <gridHelper args={[14, 14, 0xd6d3cd, 0xe7e5e0]} />
        <ContactShadows
          position={[0, 0.01, 0]}
          opacity={0.28}
          scale={12}
          blur={1.8}
          far={2.5}
          color="#1c1917"
        />
      </group>

      {/* 5. Bounds 기반 자동 카메라 포커스 & 토폴로지 씬 */}
      <Bounds clip observe margin={1.25}>
        <group
          onPointerMissed={() => {
            onSelectNode(null);
          }}
        >
          <SourceTargetNodes
            run={run}
            selectedNode={selectedNode}
            onSelectNode={onSelectNode}
            onNavigateSection={onNavigateSection}
          />
          <CdcPipelineArc
            run={run}
            selectedNode={selectedNode}
            onSelectNode={onSelectNode}
            onNavigateSection={onNavigateSection}
          />
        </group>
      </Bounds>
    </>
  );
}
