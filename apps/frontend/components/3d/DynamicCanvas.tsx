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

import { ApiError } from "@/lib/workflows";
import {
  SOURCE_STATUS_LABEL,
  securityApi,
  type BlastRadiusResponse,
  type SecurityFeed,
} from "@/lib/securityApi";

const errorText = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

export function DynamicCanvas({ run, onNavigateSection }: Props) {
  const [isCollapsed, setIsCollapsed] = useState(false);
  const [selectedNode, setSelectedNode] = useState<"source" | "cdc" | "target" | null>(null);
  // 보안 경보는 클라우드 보안 서비스에서 읽은 실제 경보만 씁니다. 패널을 열 때 읽습니다.
  const [showSecurity, setShowSecurity] = useState(false);
  const [feed, setFeed] = useState<SecurityFeed | null>(null);
  const [isLoadingFeed, setIsLoadingFeed] = useState(false);
  const [blast, setBlast] = useState<BlastRadiusResponse | null>(null);
  const [quarantinedNode, setQuarantinedNode] = useState<string | null>(null);
  const [isRerouting, setIsRerouting] = useState(false);
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

  // 3D 표시는 원본 쪽 노드가 침해됐을 때만 원본 노드 위에 그립니다 (다른 노드는 보안 패널에만 표시).
  const isCompromised = Boolean(blast?.compromised_node_id?.startsWith("source_"));
  const isQuarantined = quarantinedNode !== null;
  const riskScore = isCompromised ? (blast?.max_risk_score ?? null) : null;

  const loadFeed = () => {
    setIsLoadingFeed(true);
    securityApi
      .getFeed(run.run_id)
      .then(setFeed)
      .catch((e: unknown) => setRerouteNotice(`⚠️ 보안 경보 조회 실패: ${errorText(e)}`))
      .finally(() => setIsLoadingFeed(false));
  };

  const handleToggleSecurity = () => {
    const next = !showSecurity;
    setShowSecurity(next);
    if (next && feed === null) loadFeed();
  };

  const handleBlastRadius = (alertId: string) => {
    securityApi
      .computeBlastRadius(run.run_id, alertId)
      .then((res) => {
        setBlast(res);
        setRerouteNotice(res.compromised_node_id === null ? res.reason : res.recommended_action);
      })
      .catch((e: unknown) => setRerouteNotice(`⚠️ 영향 범위 조회 실패: ${errorText(e)}`));
  };

  const handleReroute = async () => {
    if (isRerouting) return;
    const compromised = blast?.compromised_node_id ?? null;
    const target = blast?.safe_reroute_targets[0] ?? null;
    if (compromised === null || target === null) {
      setRerouteNotice("이 경보에는 이 run 의 정상 대체 경로가 없어 우회를 요청하지 않았습니다.");
      return;
    }
    // 전환(Cutover) 진행 중에는 임의 우회 방지
    if (run.state === "running" && run.current_stage === "cutover") {
      alert("전환(Cutover) 작업이 진행 중일 때는 수동 보안 우회를 실행할 수 없습니다.");
      return;
    }
    if (!window.confirm(`[${compromised}] 를 격리하고 [${target}] 로 우회하도록 Worker 에 요청할까요?`)) return;
    setIsRerouting(true);
    try {
      const res = await securityApi.executeReroute(run.run_id, compromised, target);
      setQuarantinedNode(compromised);
      setRerouteNotice(res.message);
    } catch (e) {
      setRerouteNotice(`⚠️ 우회 실패: ${errorText(e)}`);
    } finally {
      setIsRerouting(false);
    }
  };

  const handleReleaseQuarantine = async () => {
    if (isRerouting || quarantinedNode === null) return;
    if (!window.confirm(`[${quarantinedNode}] 격리를 해제할까요?`)) return;
    setIsRerouting(true);
    try {
      const res = await securityApi.releaseQuarantine(quarantinedNode);
      setQuarantinedNode(null);
      setRerouteNotice(res.message);
    } catch (e) {
      setRerouteNotice(`⚠️ 해제 실패: ${errorText(e)}`);
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
                  onClick={handleToggleSecurity}
                  className={`rounded border px-2 py-0.5 font-mono text-[11px] transition-all active:scale-[0.96] ${
                    showSecurity
                      ? "border-red-600 bg-red-600 text-white shadow-xs"
                      : "border-ink/20 bg-paper text-ink/75 hover:border-red-500 hover:text-red-600"
                  }`}
                  title="GuardDuty·Security Command Center·Defender for Cloud 의 실제 경보를 이 run 기준으로 봅니다"
                >
                  {showSecurity ? "🛡️ 보안 경보 닫기" : "🛡️ 보안 경보"}
                </button>
                {isQuarantined && (
                  <button
                    type="button"
                    onClick={handleReleaseQuarantine}
                    disabled={isRerouting}
                    className="rounded border border-amber-600 bg-amber-500 px-2 py-0.5 font-mono text-[11px] font-semibold text-white shadow-xs transition-all hover:bg-amber-600 active:scale-[0.96] disabled:opacity-50"
                    title="격리를 해제하고 Worker 에 반영합니다"
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

      {showSecurity && (
        <div className="space-y-2 border-b border-ink/15 bg-paper/50 px-4 py-2 text-xs">
          <div className="flex flex-wrap items-center gap-1.5">
            {isLoadingFeed && <span className="text-ink/50">보안 경보 조회 중…</span>}
            {feed?.sources.map((src) => (
              <span
                key={`${src.provider}-${src.scope ?? ""}`}
                title={src.message ?? undefined}
                className={`rounded border px-1.5 py-0.5 font-mono text-[11px] ${
                  src.status === "ok" ? "border-emerald-300 bg-emerald-50 text-emerald-900" : "border-amber-300 bg-amber-50 text-amber-900"
                }`}
              >
                {src.service}
                {src.scope ? ` (${src.scope})` : ""} · {SOURCE_STATUS_LABEL[src.status]}
                {src.status === "ok" ? ` ${src.count ?? 0}건 확인` : ""}
              </span>
            ))}
            {feed && (
              <button type="button" onClick={loadFeed} className="text-[11px] text-ink/60 underline hover:text-ink">
                다시 조회
              </button>
            )}
          </div>
          {feed && feed.alerts.length === 0 && (
            <p className="text-ink/60">
              {feed.sources.some((s) => s.status === "ok")
                ? "확인한 클라우드에서 활성 경보가 없습니다. 확인하지 못한 클라우드는 위 표시를 보세요 (0건과 다릅니다)."
                : "경보를 확인한 클라우드가 없습니다 (0건이 아님). 위 표시의 이유를 해결하면 다시 조회하세요."}
            </p>
          )}
          {feed && feed.alerts.length > 0 && (
            <ul className="max-h-40 space-y-1 overflow-y-auto">
              {feed.alerts.map((alert) => (
                <li key={alert.id} className="flex flex-wrap items-center justify-between gap-2 rounded border border-ink/10 bg-white px-2 py-1">
                  <span className="font-mono text-[11px]">
                    <strong className={alert.severity === "CRITICAL" || alert.severity === "HIGH" ? "text-red-700" : "text-ink"}>
                      {alert.severity}
                    </strong>{" "}
                    {alert.provider.toUpperCase()} · {alert.threat_category} · {alert.resource_id}
                  </span>
                  <button
                    type="button"
                    onClick={() => handleBlastRadius(alert.id)}
                    className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${
                      blast?.root_alert.id === alert.id ? "border-red-600 bg-red-600 text-white" : "border-ink/20 hover:border-red-500"
                    }`}
                  >
                    영향 범위
                  </button>
                </li>
              ))}
            </ul>
          )}
          {blast && blast.compromised_node_id !== null && (
            <p className="font-mono text-[11px] text-ink/75">
              영향 노드:{" "}
              {blast.impacted_nodes
                .map((n) => `${n.name}(${n.hop_distance}홉 ${(n.risk_score * 100).toFixed(0)}%)`)
                .join(" → ")}
            </p>
          )}
        </div>
      )}

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
