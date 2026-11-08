"use client";

import { Edges, Html, Line } from "@react-three/drei";
import type { EdgeSnapshot, Topology, TopologyEdge, TopologyNode } from "@/lib/edgeLive";
import { STATE_LABEL, STATE_TONE } from "@/lib/labels";
import type { WorkflowState } from "@/lib/workflows";
import { useState } from "react";
import { DETAIL_CARD_CLASS, DETAIL_CARD_STYLE, pinAwayFromNode } from "./pinnedCard";

// 실측 토폴로지 그래프. 노드·간선은 GET /runs/{id}/topology 응답만 그립니다.
// 값을 모르면 "미제공"으로 적고, 고정 노드나 고정 지연 값을 넣지 않습니다.

interface Props {
  topology: Topology | null;
  /** edge_worker 노드에 붙일 실시간 에지 상태. 없으면 미제공으로 보입니다. */
  edge: EdgeSnapshot | null;
  fetchError?: string | null;
  /** 원본·CDC·목적지 노드 선택. 상세 카드는 한 번에 하나만 열려 겹치지 않게 합니다. */
  selectedNode?: "source" | "cdc" | "target" | null;
  onSelectNode?: (node: "source" | "cdc" | "target" | null) => void;
}

const MISSING = "미제공";
const ROW_Z = 1.9;
const ROW_Y = -0.05;

type Vec3 = [number, number, number];

// 배치만 고정합니다. 어떤 노드가 있는지는 응답이 정합니다.
const NODE_POSITION: Record<string, Vec3> = {
  source_app: [-3.9, ROW_Y, ROW_Z],
  source_db: [-2.55, ROW_Y, ROW_Z],
  vpn: [-1.25, ROW_Y, ROW_Z],
  cdc_agent: [0.05, ROW_Y, ROW_Z],
  target_db: [2.0, ROW_Y, ROW_Z],
  target_app: [3.9, ROW_Y, ROW_Z],
  edge_worker: [0, 1.3, ROW_Z],
};

// 모르는 id 는 앞줄에 차례로 놓습니다.
const fallbackPosition = (index: number): Vec3 => [-3.9 + index * 1.3, ROW_Y, ROW_Z + 1.1];

const STATE_COLOR: Record<WorkflowState, string> = {
  pending: "#d6d3cd",
  running: "#0284c7",
  waiting_approval: "#d97706",
  paused: "#d97706",
  succeeded: "#047857",
  failed: "#be123c",
  aborted: "#78716c",
};

const EDGE_COLOR: Record<TopologyEdge["kind"], string> = {
  app_db: "#57534e",
  replication: "#4338ca",
  traffic: "#c2410c",
};

const isWorkflowState = (value: string | null): value is WorkflowState =>
  value !== null && Object.prototype.hasOwnProperty.call(STATE_LABEL, value);

function NodeShape({ node, color }: { node: TopologyNode; color: string }) {
  switch (node.kind) {
    case "app":
      return (
        <mesh castShadow receiveShadow>
          <boxGeometry args={[0.5, 0.62, 0.5]} />
          <meshStandardMaterial color={color} roughness={0.55} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
      );
    case "database":
      return (
        <mesh castShadow receiveShadow>
          <cylinderGeometry args={[0.25, 0.25, 0.6, 20]} />
          <meshStandardMaterial color={color} roughness={0.5} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
      );
    case "vpn":
      return (
        <mesh castShadow>
          <octahedronGeometry args={[0.24]} />
          <meshStandardMaterial color={color} roughness={0.5} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
      );
    case "edge":
      return (
        <mesh castShadow>
          <cylinderGeometry args={[0.32, 0.32, 0.16, 6]} />
          <meshStandardMaterial color={color} roughness={0.4} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
      );
    default:
      return (
        <mesh castShadow>
          <cylinderGeometry args={[0.2, 0.2, 0.42, 14]} />
          <meshStandardMaterial color={color} roughness={0.55} />
          <Edges color="#1c1917" threshold={15} />
        </mesh>
      );
  }
}

/** edge_worker 노드에 붙이는 실시간 줄. 프로브가 측정한 지연만 씁니다. */
function edgeLiveLines(edge: EdgeSnapshot | null): string[] {
  if (!edge) return [`게이트 ${MISSING}`, `지연 ${MISSING}`];
  const { worker, probe } = edge;
  const gate = worker.status === "ok" ? (worker.mode ?? MISSING) : MISSING;
  let latency = probe.status === "skipped" ? "HOLD 중 측정 안 함" : MISSING;
  if (probe.status === "ok" && probe.latency_ms !== null) {
    latency = `${probe.latency_ms.toFixed(1)}ms${probe.ok === false ? " (응답 실패)" : ""}`;
  }
  const origin = probe.status === "ok" ? (probe.origin ?? MISSING) : MISSING;
  return [`게이트 ${gate} · 응답 출처 ${origin}`, `프로브 지연 ${latency}`];
}

const MISSING_TONE = "bg-stone-100 text-stone-600 ring-stone-300";

/** 노드 옆에 늘 보이는 작은 칩. 이름과 상태만 보이고, 누르면 상세 카드를 엽니다. */
function NodeChip({ node, selected, onToggle }: { node: TopologyNode; selected: boolean; onToggle: () => void }) {
  const state = isWorkflowState(node.state) ? node.state : null;
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
      aria-pressed={selected}
      title={`${node.label} 상세 실측 보기`}
      className={`pointer-events-auto flex max-w-[7.5rem] items-center gap-1 whitespace-nowrap rounded border bg-white/95 px-1 py-px font-mono text-[9px] leading-tight text-ink transition-transform hover:-translate-y-0.5 ${
        selected ? "border-ink shadow-[1px_1px_0_0_var(--color-ink)]" : "border-ink/40"
      }`}
    >
      <span className="truncate font-semibold">{node.label}</span>
      <span className={`shrink-0 rounded px-0.5 ring-1 ${state ? STATE_TONE[state] : MISSING_TONE}`}>
        {state ? STATE_LABEL[state] : MISSING}
      </span>
    </button>
  );
}

/** 선택한 노드의 실측 상세. 캔버스 아래 모서리(노드 반대편)에 고정합니다. */
function NodeDetailCard({ node, extraLines, onClose }: { node: TopologyNode; extraLines: string[]; onClose: () => void }) {
  const state = isWorkflowState(node.state) ? node.state : null;
  return (
    <div className={`${DETAIL_CARD_CLASS} -translate-y-full font-mono text-[10px] leading-snug`} style={DETAIL_CARD_STYLE}>
      <div className="flex items-center justify-between gap-1 border-b border-ink/15 pb-1">
        <span className="truncate font-semibold">{node.label}</span>
        <span className={`shrink-0 rounded px-1 text-[9px] ring-1 ${state ? STATE_TONE[state] : MISSING_TONE}`}>
          {state ? STATE_LABEL[state] : MISSING}
        </span>
        <button type="button" onClick={onClose} aria-label="상세 닫기" className="shrink-0 px-0.5 text-ink/50 hover:text-ink">
          ✕
        </button>
      </div>
      <dl className="mt-1 space-y-0.5">
        <div className="flex justify-between gap-2">
          <dt className="shrink-0 text-ink/55">위치</dt>
          <dd className="truncate">{`${node.cloud ?? MISSING} · ${node.region ?? MISSING}`}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="shrink-0 text-ink/55">리소스</dt>
          <dd className="truncate" title={node.resource ?? undefined}>{node.resource ?? MISSING}</dd>
        </div>
      </dl>
      {extraLines.map((line) => (
        <p key={line} className="mt-0.5 truncate text-orange-900" title={line}>{line}</p>
      ))}
    </div>
  );
}

export function TopologyGraphNodes({ topology, edge, fetchError, selectedNode = null, onSelectNode }: Props) {
  const [chosenId, setChosenId] = useState<string | null>(null);
  // 원본·CDC·목적지 노드가 선택되면 그 카드가 우선이고, 토폴로지 상세는 닫힌 것으로 봅니다.
  const selectedId = selectedNode === null ? chosenId : null;
  const toggleNode = (id: string) => {
    const next = selectedId === id ? null : id;
    setChosenId(next);
    if (next !== null) onSelectNode?.(null);
  };

  if (!topology) {
    return (
      <Html position={[0, ROW_Y, ROW_Z]} center zIndexRange={[20, 0]}>
        <div className="whitespace-nowrap rounded border border-ink/30 bg-white px-2 py-0.5 font-mono text-[10px] text-ink/70">
          {fetchError ? `토폴로지 조회 실패: ${fetchError}` : "토폴로지 불러오는 중…"}
        </div>
      </Html>
    );
  }

  const positions = new Map<string, Vec3>();
  let unknownIndex = 0;
  for (const node of topology.nodes) {
    positions.set(node.id, NODE_POSITION[node.id] ?? fallbackPosition(unknownIndex++));
  }

  const edgePoints = (e: TopologyEdge): Vec3[] | null => {
    const from = positions.get(e.from);
    const to = positions.get(e.to);
    if (!from || !to) return null;
    const via = e.via ? positions.get(e.via) : undefined;
    return via ? [from, via, to] : [from, to];
  };

  return (
    <group>
      {topology.edges.map((e) => {
        const points = edgePoints(e);
        if (!points) return null;
        return (
          <Line
            key={e.id}
            points={points}
            color={EDGE_COLOR[e.kind] ?? "#57534e"}
            lineWidth={e.kind === "replication" ? 2 : 1.4}
            dashed={e.kind === "traffic"}
            dashSize={0.12}
            gapSize={0.08}
          />
        );
      })}

      {topology.nodes.map((node, index) => {
        const position = positions.get(node.id) ?? fallbackPosition(0);
        const color = isWorkflowState(node.state) ? STATE_COLOR[node.state] : STATE_COLOR.pending;
        const extraLines = node.kind === "edge" ? edgeLiveLines(edge) : [];
        return (
          <group key={node.id} position={position}>
            <NodeShape node={node} color={color} />
            {/* 칩이 겹치지 않게 위·아래를 번갈아 놓습니다. 크기는 화면 기준으로 고정합니다 (확대해도 커지지 않음). */}
            <Html
              position={[0, node.kind === "edge" || index % 2 === 1 ? 0.5 : -0.5, 0]}
              center
              zIndexRange={[30, 10]}
            >
              <NodeChip
                node={node}
                selected={selectedId === node.id}
                onToggle={() => toggleNode(node.id)}
              />
            </Html>
            {selectedId === node.id && (
              <Html calculatePosition={pinAwayFromNode("bottom")} zIndexRange={[90, 40]}>
                <NodeDetailCard node={node} extraLines={extraLines} onClose={() => setChosenId(null)} />
              </Html>
            )}
          </group>
        );
      })}
    </group>
  );
}
