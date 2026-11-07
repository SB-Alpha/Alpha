"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useRef, useState } from "react";
import { useEdgeLive, useRunTopology } from "@/lib/edgeLive";
import { extractCdcArcData, extractSourceNodeData, extractTargetNodeData } from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";
import type { OrbitControlsHandle } from "./CloudMorphScene";

interface Props {
  run: RunStatus;
  onNavigateSection?: (sectionId: string) => void;
}

const CanvasView = dynamic(() => import("./CanvasView"), {
  ssr: false,
  loading: () => (
    <div className="flex h-56 w-full items-center justify-center bg-paper font-mono text-xs text-ink/50">
      토폴로지 뷰포트 불러오는 중…
    </div>
  ),
});

import { securityApi } from "@/lib/securityApi";

// 영향 범위 조회는 보안 모듈의 고정 노드 id 로 부릅니다. 이 run 의 원본에서 나온 값이 아니므로 화면에 id 를 그대로 밝힙니다.
// 토폴로지에서 노드를 고르는 연동은 PR-6(보안)에서 합니다.
const BLAST_RADIUS_NODE_ID = "aws-ecs-workitems";

export function DynamicCanvas({ run, onNavigateSection }: Props) {
  const [isCollapsed, setIsCollapsed] = useState(false);
  const [selectedNode, setSelectedNode] = useState<"source" | "cdc" | "target" | null>(null);
  const [isCompromised, setIsCompromised] = useState(false);
  const [isQuarantined, setIsQuarantined] = useState(false);
  const [isRerouting, setIsRerouting] = useState(false);
  const [isCheckingBlastRadius, setIsCheckingBlastRadius] = useState(false);
  // 보안 API 가 돌려준 값만 씁니다. 응답 전이나 값이 없으면 null 입니다.
  const [riskScore, setRiskScore] = useState<number | null>(null);
  const [showFullTopology, setShowFullTopology] = useState(false);
  const [rerouteNotice, setRerouteNotice] = useState<string | null>(null);
  const controlsRef = useRef<OrbitControlsHandle | null>(null);

  // 실측 토폴로지는 그래프를 켰을 때만 조회합니다. 에지 상태는 Worker 노드가 있을 때만 폴링합니다
  // (라이브 프로브 요청이 게이트를 실제로 지나가므로 필요할 때만 부릅니다).
  const { topology, fetchError: topologyError } = useRunTopology(run.run_id, showFullTopology);
  const hasEdgeWorker = Boolean(topology?.nodes.some((n) => n.id === "edge_worker"));
  const { snapshot: edgeLive } = useEdgeLive(1500, showFullTopology && hasEdgeWorker);

  const source = extractSourceNodeData(run);
  const cdc = extractCdcArcData(run);
  const target = extractTargetNodeData(run);

  const handleResetView = () => {
    setSelectedNode(null);
    controlsRef.current?.reset();
  };

  // 영향 범위는 보안 API(/security/blast-radius) 응답이 숫자 위험도를 돌려줄 때만 3D 로 표시합니다.
  // 로컬에서 침해 상태를 켜는 데모 토글은 없앴습니다. 보안 연동 정리는 PR-6 에서 합니다.
  const handleToggleBlastRadius = () => {
    if (isCheckingBlastRadius) return;
    if (isCompromised) {
      setIsCompromised(false);
      setIsQuarantined(false);
      setRiskScore(null);
      setRerouteNotice(null);
      return;
    }
    setIsCheckingBlastRadius(true);
    securityApi.computeBlastRadius(BLAST_RADIUS_NODE_ID)
      .then((res) => {
        const score = typeof res?.max_risk_score === "number" && Number.isFinite(res.max_risk_score)
          ? res.max_risk_score
          : null;
        setRiskScore(score);
        setIsCompromised(score !== null);
        if (score === null) setRerouteNotice("영향 범위 조회: 위험도 미제공");
      })
      .catch((cause) => {
        setRiskScore(null);
        setIsCompromised(false);
        setRerouteNotice(`⚠️ 영향 범위 조회 실패: ${cause instanceof Error ? cause.message : "unknown"}`);
      })
      .finally(() => setIsCheckingBlastRadius(false));
  };

  const handleReroute = async () => {
    if (isRerouting) return;

    // 전환(Cutover) 진행 중에는 임의 우회 방지
    if (run.state === "running" && run.current_stage === "cutover") {
      alert("전환(Cutover) 작업이 진행 중일 때는 수동 보안 우회를 실행할 수 없습니다.");
      return;
    }

    const confirmed = window.confirm(
      "영향 범위 조회 대상 노드를 격리(Quarantine)하고 트래픽 우회를 요청하시겠습니까?\n\n※ 데이터 정합성 보호를 위해 원본 쓰기는 503 차단되며 읽기 트래픽만 우회됩니다."
    );
    if (!confirmed) return;

    setIsRerouting(true);
    try {
      const res = await fetch("/api/security/reroute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          compromised_node_id: "aws-ecs-workitems",
          target_reroute_id: "gcp-cloud-run",
          quarantine: true,
          run_id: run.run_id,
        }),
      });

      if (res.ok) {
        const data = await res.json();
        setIsQuarantined(true);
        const prefix = data.cloudflare_synced ? "Cloudflare 에지: " : "데모 시뮬레이션: ";
        setRerouteNotice(`${prefix}${data.message}`);
      } else {
        const errData = await res.json().catch(() => null);
        const errDetail = errData?.detail || `HTTP ${res.status}`;
        setIsQuarantined(false);
        setRerouteNotice(`⚠️ 우회 실패: ${errDetail}`);
      }
    } catch (cause) {
      setIsQuarantined(false);
      setRerouteNotice(`⚠️ 우회 실패: 네트워크 오류 (${cause instanceof Error ? cause.message : "unknown"})`);
    } finally {
      setIsRerouting(false);
    }
  };

  const handleReleaseQuarantine = async () => {
    if (isRerouting) return;
    const confirmed = window.confirm("보안 격리를 해제하고 정상 원본 트래픽(AWS)으로 롤백 복원하시겠습니까?");
    if (!confirmed) return;

    setIsRerouting(true);
    try {
      const res = await fetch("/api/security/release", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node_id: "aws-ecs-workitems" }),
      });

      if (res.ok) {
        const data = await res.json();
        setIsQuarantined(false);
        const prefix = data.cloudflare_synced ? "Cloudflare 에지: " : "데모 시뮬레이션: ";
        setRerouteNotice(`${prefix}${data.message}`);
      } else {
        const errData = await res.json().catch(() => null);
        setRerouteNotice(`⚠️ 해제 실패: ${errData?.detail || `HTTP ${res.status}`}`);
      }
    } catch (cause) {
      setRerouteNotice(`⚠️ 해제 실패: 네트워크 오류 (${cause instanceof Error ? cause.message : "unknown"})`);
    } finally {
      setIsRerouting(false);
    }
  };

  return (
    <section
      aria-label="3D 인프라 토폴로지 및 CDC 파이프라인"
      className="overflow-hidden rounded-lg border border-ink bg-white shadow-[3px_3px_0_0_var(--color-ink)]"
    >
      {/* 상단 컨트롤 바 — 운영 콘솔 종이·잉크 톤 */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink/15 bg-white px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="font-semibold text-ink">토폴로지 & CDC 경로</span>
          <span className="text-ink/25">·</span>
          <span className="font-mono text-ink/65">
            {source.cloudName} {source.region ?? "미제공"} → {target.providerSummary}
          </span>
          {cdc.tables.length > 0 && (
            <span className="rounded border border-ink/15 bg-paper px-1.5 py-0.5 font-mono text-[11px] text-ink/70">
              테이블 {cdc.tables.length}개 ({cdc.tables.filter((t) => t.match).length}/{cdc.tables.length} 일치)
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {!isCollapsed && (
            <>
              <div className="hidden items-center gap-1 sm:flex">
                <button
                  type="button"
                  onClick={handleResetView}
                  className={`rounded border px-2 py-0.5 font-mono text-[11px] transition-all active:scale-[0.96] ${
                    selectedNode === null
                      ? "border-ink bg-ink text-paper shadow-xs"
                      : "border-ink/20 bg-paper text-ink/75 hover:border-ink hover:text-ink"
                  }`}
                >
                  🌐 전체 조감도
                </button>
                {(
                  [
                    { key: "source", label: `☁️ 원본 ${source.cloudName}` },
                    { key: "cdc", label: "⚡ CDC 아치" },
                    { key: "target", label: `🚀 목적지 ${target.providerSummary}` },
                  ] as const
                ).map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    onClick={() => setSelectedNode((prev) => (prev === item.key ? null : item.key))}
                    className={`rounded border px-2 py-0.5 font-mono text-[11px] transition-all active:scale-[0.96] ${
                      selectedNode === item.key
                        ? "border-ink bg-ink text-paper shadow-xs"
                        : "border-ink/20 bg-paper text-ink/75 hover:border-ink"
                    }`}
                  >
                    {item.label}
                  </button>
                ))}
                <button
                  type="button"
                  onClick={handleToggleBlastRadius}
                  disabled={isCheckingBlastRadius}
                  className={`rounded border px-2 py-0.5 font-mono text-[11px] transition-all active:scale-[0.96] disabled:opacity-50 ${
                    isCompromised
                      ? "border-red-600 bg-red-600 text-white shadow-xs"
                      : "border-ink/20 bg-paper text-ink/75 hover:border-red-500 hover:text-red-600"
                  }`}
                  title={`보안 API(/security/blast-radius)에 고정 노드 ${BLAST_RADIUS_NODE_ID} 의 영향 범위를 조회하고, 숫자 위험도가 오면 3D 로 표시합니다. 이 run 의 원본과 별개인 값입니다`}
                >
                  {isCheckingBlastRadius ? "조회 중…" : isCompromised ? "🛡️ 영향 범위 표시 끄기" : "🛡️ 영향 범위 조회"}
                </button>
                {isQuarantined && (
                  <button
                    type="button"
                    onClick={handleReleaseQuarantine}
                    disabled={isRerouting}
                    className="rounded border border-amber-600 bg-amber-500 px-2 py-0.5 font-mono text-[11px] font-semibold text-white shadow-xs transition-all hover:bg-amber-600 active:scale-[0.96] disabled:opacity-50"
                    title="격리 상태를 해제하고 원본 트래픽(AWS)으로 롤백 복원"
                  >
                    {isRerouting ? "해제 중..." : "↩️ 격리 해제 (Rollback)"}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => setShowFullTopology((prev) => !prev)}
                  className={`rounded border px-2 py-0.5 font-mono text-[11px] transition-all active:scale-[0.96] ${
                    showFullTopology
                      ? "border-sky-700 bg-sky-700 text-white shadow-xs"
                      : "border-ink/20 bg-paper text-ink/75 hover:border-sky-600 hover:text-sky-700"
                  }`}
                  title="이 run 의 실측 토폴로지(앱·DB·CDC·VPN·Worker)를 그래프로 그립니다"
                >
                  {showFullTopology ? "🧭 실측 토폴로지 켜짐" : "🧭 실측 토폴로지"}
                </button>
              </div>
            </>
          )}

          <Link
            href={`/runs/${encodeURIComponent(run.run_id)}/report`}
            className="rounded border border-ink bg-ink px-2.5 py-0.5 text-[11px] font-medium text-paper transition-transform hover:-translate-y-px"
          >
            시각적 요약 리포트 →
          </Link>

          <button
            type="button"
            onClick={() => setIsCollapsed((prev) => !prev)}
            className="rounded border border-ink/20 bg-white px-2 py-0.5 text-[11px] font-medium text-ink/70 hover:border-ink hover:text-ink"
          >
            {isCollapsed ? "토폴로지 펼치기" : "접기"}
          </button>
        </div>
      </div>

      {rerouteNotice && (
        <div className="flex items-center justify-between border-b border-amber-300 bg-amber-50 px-4 py-1.5 text-xs font-mono text-amber-900">
          <span>⚡ {rerouteNotice}</span>
          <button
            type="button"
            onClick={() => setRerouteNotice(null)}
            className="text-amber-700 hover:underline"
          >
            닫기 ✕
          </button>
        </div>
      )}

      {/* 컴팩트 3D 뷰포트 (Step 1) */}
      {!isCollapsed && (
        <div className="relative h-60 w-full bg-paper">
          <CanvasView
            run={run}
            controlsRef={controlsRef}
            selectedNode={selectedNode}
            onSelectNode={setSelectedNode}
            onNavigateSection={onNavigateSection}
            isCompromised={isCompromised}
            isQuarantined={isQuarantined}
            riskScore={isCompromised ? riskScore : null}
            onReroute={handleReroute}
            showFullTopology={showFullTopology}
            topology={topology}
            topologyError={topologyError}
            edgeLive={edgeLive}
          />

          {/* 하단 실측 데이터 요약 바 */}
          <div className="pointer-events-none absolute inset-x-3 bottom-2 flex flex-wrap items-center justify-between gap-2 text-[11px]">
            <span className="flex items-center gap-1.5 rounded border border-ink/15 bg-white/90 px-2 py-0.5 font-mono text-ink/70 backdrop-blur-xs shadow-xs">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-600 animate-pulse" />
              노드 클릭 시 카메라 자동 포커스(Bounds) 및 상세 실측 카드 연동
            </span>
            <div className="flex items-center gap-2 font-mono">
              <span className="rounded border border-ink/15 bg-white/90 px-2 py-0.5 text-ink tabular-nums shadow-xs">
                복사율 <strong className="font-semibold text-emerald-700">{cdc.copyPercent !== null ? `${cdc.copyPercent}%` : "미제공"}</strong>
              </span>
              <span className="rounded border border-ink/15 bg-white/90 px-2 py-0.5 text-ink tabular-nums shadow-xs">
                복제 지연 <strong className="font-semibold text-indigo-900">{cdc.lagFormatted ?? "미제공"}</strong>
              </span>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
