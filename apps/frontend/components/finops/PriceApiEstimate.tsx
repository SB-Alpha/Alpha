"use client";

import { useState } from "react";
import { NOT_PROVIDED, cloudLabel, type TargetProvider } from "@/lib/finops";
import type { ServingSide } from "@/lib/dashboard";
import type {
  ArbitrageResult,
  CloudCost,
  Crossover,
  EstimateState,
  FinopsEstimate,
  Saving,
  TrafficInputs,
} from "@/lib/finopsApi";

// 백엔드 /finops/estimate 결과만 그립니다. 금액·교차점·차익거래는 모두 백엔드가 가격 API 단가로 낸 값이고,
// 화면에서 다시 계산하지 않습니다. 입력을 바꾸면 백엔드를 다시 부릅니다. 모르는 금액은 "미제공" 입니다.

const LINE_COLORS: Record<string, string> = {
  "앱 컴퓨팅": "bg-ink",
  "DB 컴퓨팅": "bg-indigo-700",
  "DB 스토리지": "bg-ink/30",
};

const ESTIMATE_LABEL = "가격 API 견적";

/** 실제 RunStatus 에서 읽은 실시간 지표 (가상 생성값 없음) */
export interface RealRunTelemetry {
  progress: number;
  elapsedText: string;
  stageLabel: string;
  replicationLagBytes: number | null;
  servingSide: ServingSide | null;
}

const usd = (v: number | null | undefined) =>
  v === null || v === undefined
    ? NOT_PROVIDED
    : `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const usdPerGb = (v: number) =>
  `${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 4 })}/GB`;

const signedUsd = (v: number | null) =>
  v === null ? NOT_PROVIDED : `${v > 0 ? "+" : v < 0 ? "-" : ""}${usd(Math.abs(v))}`;

function lineTotal(saving: Saving | undefined, sourceTotal: number | null): number | null {
  if (saving === undefined) return sourceTotal;
  return saving.complete ? saving.candidate_monthly_usd : null;
}

function savingText(s: Saving | undefined): string {
  if (!s || s.monthly_saving_usd === null) return "비교 불가 (미제공 항목 있음)";
  const pct = s.saving_pct === null ? "" : ` (${s.saving_pct > 0 ? "-" : "+"}${Math.abs(s.saving_pct)}%)`;
  return s.monthly_saving_usd >= 0
    ? `월 ${usd(s.monthly_saving_usd)} 절감${pct}`
    : `월 ${usd(-s.monthly_saving_usd)} 증가${pct}`;
}

function arbitrageStatus(arb: ArbitrageResult): string {
  if (arb.lag_fenced && arb.recommendation.startsWith(NOT_PROVIDED)) return "복제 지연 미제공 · 원본 고정";
  if (arb.lag_fenced) return "복제 지연으로 원본 고정";
  if (
    arb.recommendation.startsWith(NOT_PROVIDED) ||
    arb.read_weight_target === null ||
    arb.read_weight_source === null
  ) {
    return NOT_PROVIDED;
  }
  return `읽기 목적지 ${Math.round(arb.read_weight_target * 100)}% · 원본 ${Math.round(arb.read_weight_source * 100)}%`;
}

/**
 * 상단 핵심 영역 우측: 항상 한눈에 보이는 컴팩트 실시간 비용 교차 그래프
 */
function CompactCrossoverChart({
  x,
  arb,
  traffic,
  onChangeRps,
  onChangeIdlePercent,
}: {
  x: Crossover | null;
  arb: ArbitrageResult | null | undefined;
  traffic?: TrafficInputs;
  onChangeRps?: (rps: number) => void;
  onChangeIdlePercent?: (idle: number) => void;
}) {
  const [axisMode, setAxisMode] = useState<"rps" | "idle">("rps");

  // 트래픽 입력(RPS·유휴율·처리 시간)이 없으면 짐작하지 않고 그래프를 그리지 않습니다.
  // 곡선은 백엔드가 준 단가(Fargate 월, Cloud Run 1대 월, 동시 처리, 최대 대수)에 입력값을 대입해 그립니다.
  if (
    !traffic ||
    traffic.rps === null ||
    traffic.idlePercent === null ||
    traffic.avgLatencyMs === null ||
    !x ||
    x.fargate_app_monthly_usd === null ||
    x.cloud_run_instance_monthly_usd === null
  ) {
    return (
      <div className="flex h-full items-center justify-center rounded-lg border border-ink/15 bg-paper/40 p-3 text-xs text-ink/50">
        교차점 그래프: {NOT_PROVIDED}
      </div>
    );
  }

  const fargate = x.fargate_app_monthly_usd;
  const crFull = x.cloud_run_instance_monthly_usd;
  const beIdle = x.break_even_idle_percent;
  const rpsNow = traffic.rps;
  const idleNow = traffic.idlePercent;
  const latencySec = traffic.avgLatencyMs / 1000;
  const readRatio = traffic.readRatio;
  const activeShare = 1 - idleNow / 100;

  const W = 440;
  const H = 128;
  const PAD_L = 38;
  const PAD_R = 14;
  const PAD_T = 14;
  const PAD_B = 22;
  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;

  const MAX_RPS = 200;
  const crOneActive = crFull * activeShare;
  const arbBase = arb?.baseline_monthly_cost ?? fargate;
  const arbCrossRps = arb?.crossover_rps ?? null;
  const isFenced = arb?.lag_fenced ?? false;

  const costAtRps = (r: number) => {
    if (r <= 0) return 0;
    const needed = Math.min(x.max_instances, Math.max(1, Math.ceil((r * latencySec) / x.concurrency)));
    return needed * crOneActive;
  };

  // 읽기 비율을 모르면 차익거래 곡선을 그리지 않습니다 (예전 0.8 고정값 없음)
  const arbCostAtRps = (r: number) => {
    if (!arb || arbBase <= 0 || readRatio === null) return null;
    if (isFenced) return arbBase;
    const writeShareCost = arbBase * (1 - readRatio);
    if (arbCrossRps && arbCrossRps > 0) {
      const readCost = (r / arbCrossRps) * (arbBase * readRatio);
      return Math.min(arbBase, writeShareCost + readCost);
    }
    return null;
  };

  const maxCost =
    axisMode === "rps"
      ? Math.max(fargate * 1.25, crOneActive * x.max_instances * 1.1, 10)
      : Math.max(fargate, crFull) * 1.15;

  const yOf = (c: number) => PAD_T + plotH - Math.max(0, Math.min(1, c / maxCost)) * plotH;
  const xOfRps = (r: number) => PAD_L + (Math.max(0, Math.min(MAX_RPS, r)) / MAX_RPS) * plotW;
  const xOfIdle = (idl: number) => PAD_L + (Math.max(0, Math.min(80, idl)) / 80) * plotW;

  const rpsPoints: string[] = [];
  const arbPoints: string[] = [];
  if (axisMode === "rps") {
    for (let r = 0; r <= MAX_RPS; r += 2) {
      rpsPoints.push(`${xOfRps(r).toFixed(1)},${yOf(costAtRps(r)).toFixed(1)}`);
      const ac = arbCostAtRps(r);
      if (ac !== null) arbPoints.push(`${xOfRps(r).toFixed(1)},${yOf(ac).toFixed(1)}`);
    }
  }

  const currentCostCr = axisMode === "rps" ? costAtRps(rpsNow) : crFull * (1 - idleNow / 100);

  const handleChartClick = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const relX = ((e.clientX - rect.left) / rect.width) * W;
    const ratio = Math.max(0, Math.min(1, (relX - PAD_L) / plotW));
    if (axisMode === "rps" && onChangeRps) {
      onChangeRps(Math.max(1, Math.round((ratio * MAX_RPS) / 5) * 5));
    } else if (axisMode === "idle" && onChangeIdlePercent) {
      onChangeIdlePercent(Math.round((ratio * 80) / 5) * 5);
    }
  };

  return (
    <div className="rounded-lg border border-ink/20 bg-paper/40 p-3 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-1.5">
        <div className="flex items-center gap-2 font-mono text-[11px]">
          <span className="font-semibold text-ink">Cloud Run ↔ Fargate 비용 곡선</span>
          <span className="text-[10px] text-ink/55">
            (교차: {x.break_even_rps !== null ? `≤${x.break_even_rps} RPS` : `유휴 >${beIdle ?? "—"}%`})
          </span>
          <span className="rounded border border-amber-300 bg-amber-50 px-1 text-[9px] text-amber-800">{ESTIMATE_LABEL}</span>
        </div>
        <div className="inline-flex rounded border border-ink/20 bg-white p-0.5 font-mono text-[10px]">
          <button
            type="button"
            onClick={() => setAxisMode("rps")}
            className={`rounded px-1.5 py-0.5 ${axisMode === "rps" ? "bg-ink font-semibold text-white" : "text-ink/65"}`}
          >
            RPS
          </button>
          <button
            type="button"
            onClick={() => setAxisMode("idle")}
            className={`rounded px-1.5 py-0.5 ${axisMode === "idle" ? "bg-ink font-semibold text-white" : "text-ink/65"}`}
          >
            유휴%
          </button>
        </div>
      </div>

      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="mt-1.5 w-full cursor-pointer select-none overflow-visible"
        onClick={handleChartClick}
        role="img"
        aria-label="비용 교차 그래프"
      >
        {[0, 0.5, 1].map((ratio) => {
          const val = maxCost * ratio;
          const y = yOf(val);
          return (
            <g key={ratio}>
              <line x1={PAD_L} y1={y} x2={W - PAD_R} y2={y} stroke="#1c1917" strokeOpacity="0.1" />
              <text x={PAD_L - 4} y={y + 3} textAnchor="end" className="fill-ink/50 font-mono text-[8px]">
                ${Math.round(val)}
              </text>
            </g>
          );
        })}

        <line
          x1={PAD_L}
          y1={yOf(fargate)}
          x2={W - PAD_R}
          y2={yOf(fargate)}
          stroke="#1c1917"
          strokeOpacity="0.6"
          strokeWidth="1.3"
          strokeDasharray="3 2"
        />

        {axisMode === "rps" ? (
          <>
            <polyline fill="none" stroke="#3730a3" strokeWidth="2" points={rpsPoints.join(" ")} />
            {arbPoints.length > 0 && (
              <polyline
                fill="none"
                stroke="#047857"
                strokeWidth="1.6"
                strokeDasharray="2 2"
                points={arbPoints.join(" ")}
              />
            )}
            <line
              x1={xOfRps(rpsNow)}
              y1={PAD_T}
              x2={xOfRps(rpsNow)}
              y2={PAD_T + plotH}
              stroke="#3730a3"
              strokeWidth="1"
              strokeOpacity="0.5"
            />
            <circle cx={xOfRps(rpsNow)} cy={yOf(currentCostCr)} r="4" fill="#3730a3" stroke="#fff" strokeWidth="1.2" />
            {[0, 50, 100, 150, 200].map((r) => (
              <text key={r} x={xOfRps(r)} y={H - 4} textAnchor="middle" className="fill-ink/55 font-mono text-[8px]">
                {r}
              </text>
            ))}
          </>
        ) : (
          <>
            <line
              x1={xOfIdle(0)}
              y1={yOf(crFull)}
              x2={xOfIdle(80)}
              y2={yOf(crFull * (1 - 80 / 100))}
              stroke="#3730a3"
              strokeWidth="2"
            />
            <line
              x1={xOfIdle(idleNow)}
              y1={PAD_T}
              x2={xOfIdle(idleNow)}
              y2={PAD_T + plotH}
              stroke="#3730a3"
              strokeWidth="1"
              strokeOpacity="0.5"
            />
            <circle cx={xOfIdle(idleNow)} cy={yOf(currentCostCr)} r="4" fill="#3730a3" stroke="#fff" strokeWidth="1.2" />
            {[0, 20, 40, 60, 80].map((idl) => (
              <text key={idl} x={xOfIdle(idl)} y={H - 4} textAnchor="middle" className="fill-ink/55 font-mono text-[8px]">
                {idl}%
              </text>
            ))}
          </>
        )}
      </svg>

      <div className="mt-1 flex flex-wrap items-center justify-between gap-2 font-mono text-[10px] text-ink/65">
        <span>Fargate: {usd(fargate)}/월</span>
        <span className="text-indigo-800">Cloud Run: {usd(currentCostCr)}/월</span>
        {arb && <span className="text-emerald-800">차익거래: {usd(arb.arbitrage_monthly_cost)}/월</span>}
      </div>
    </div>
  );
}

/**
 * 접힘 영역 내부: 상세 3사 비교 및 차익거래 Egress 출처 (평소에는 닫혀 있음)
 */
function CollapsibleDetailedSection({
  data,
  selectedProvider,
  onSelectProvider,
}: {
  data: FinopsEstimate;
  selectedProvider: TargetProvider;
  onSelectProvider?: (p: TargetProvider) => void;
}) {
  const { input, report, arbitrage: arb } = data;
  const savingOf = (cloud: string) => report.savings.find((s) => s.cloud === cloud);
  const sourceTotal = report.savings[0]?.current_monthly_usd ?? null;

  const allClouds: { cost: CloudCost; saving?: Saving; total: number | null; isSource: boolean }[] = [
    { cost: input.current, saving: undefined, total: sourceTotal, isSource: true },
    ...input.candidates.map((c) => {
      const s = savingOf(c.cloud);
      return { cost: c, saving: s, total: lineTotal(s, sourceTotal), isSource: false };
    }),
  ];

  const validTotals = allClouds.map((c) => c.total).filter((v): v is number => v !== null && v > 0);
  const maxTotal = validTotals.length > 0 ? Math.max(...validTotals) : 0;

  return (
    <details className="mt-3 rounded-lg border border-ink/15 bg-paper/30 p-3 text-xs">
      <summary className="cursor-pointer font-semibold text-ink/80 hover:text-ink">
        상세 비교 도표 · 차익거래 Egress 출처 · 주의점 펼치기
      </summary>

      <div className="mt-3 space-y-3">
        {/* 3사 가로 누적 바 비교 */}
        <div className="grid gap-2 sm:grid-cols-3">
          {allClouds.map(({ cost, saving, total, isSource }) => {
            const isSelected = selectedProvider === cost.cloud;
            const widthPct = total !== null && maxTotal > 0 ? Math.max(8, Math.min(100, (total / maxTotal) * 100)) : 0;
            return (
              <div
                key={cost.cloud}
                onClick={() => {
                  if (onSelectProvider && (cost.cloud === "gcp" || cost.cloud === "azure" || cost.cloud === "aws")) {
                    onSelectProvider(cost.cloud);
                  }
                }}
                className={`cursor-pointer rounded border p-2.5 font-mono transition-colors ${
                  isSelected ? "border-ink bg-white" : "border-ink/15 bg-white/70 hover:border-ink/40"
                }`}
              >
                <div className="flex items-baseline justify-between">
                  <span className="text-xs font-bold text-ink">
                    {isSource ? "[원본] " : ""}
                    {cloudLabel(cost.cloud)} {cost.region ?? ""}
                  </span>
                  <span className="text-sm font-bold tabular-nums text-ink">{usd(total)}/월</span>
                </div>
                {!isSource && <p className="mt-0.5 text-[10px] text-ink/65">{savingText(saving)}</p>}
                <div className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-paper">
                  {total !== null && total > 0 && (
                    <div className="flex h-full" style={{ width: `${widthPct}%` }}>
                      {cost.lines.map((line) => {
                        // 금액을 모르는 항목은 막대에 0 으로 넣지 않고 빼 둡니다 (아래 목록에 "미제공" 으로 나옴)
                        if (line.monthly_usd === null) return null;
                        const pct = total > 0 ? (line.monthly_usd / total) * 100 : 0;
                        return pct > 0 ? (
                          <div
                            key={line.item}
                            style={{ width: `${pct}%` }}
                            className={LINE_COLORS[line.item] ?? "bg-ink/40"}
                            title={`${line.item}: ${usd(line.monthly_usd)} (${line.source})`}
                          />
                        ) : null;
                      })}
                    </div>
                  )}
                </div>
                <div className="mt-1.5 space-y-0.5 text-[10px] text-ink/70">
                  {cost.lines.map((line) => (
                    <div key={line.item} className="flex justify-between" title={line.source}>
                      <span>{line.item}</span>
                      <span className="font-semibold text-ink">{usd(line.monthly_usd)}</span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>

        {/* 차익거래 상세 근거 및 Egress API 출처 */}
        {arb && (
          <div className="rounded border border-ink/15 bg-white p-2.5 text-[11px] text-ink/75">
            <p className="font-semibold text-ink">{arb.recommendation}</p>
            <p className="mt-1 font-mono text-[10px] text-ink/60">
              egress 단가:{" "}
              {arb.egress_usd_per_gb === null
                ? NOT_PROVIDED
                : `${usdPerGb(arb.egress_usd_per_gb)} (출처: ${arb.egress_source ?? NOT_PROVIDED})`}
            </p>
            <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[11px] text-ink/65">
              {arb.reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          </div>
        )}

        {/* 계산 코드 주의점 */}
        <ul className="list-disc space-y-0.5 pl-4 text-[11px] text-ink/65">
          {report.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      </div>
    </details>
  );
}

export function PriceApiEstimate({
  state,
  selectedProvider = "gcp",
  onSelectProvider,
  traffic,
  onChangeRps,
  onChangeIdlePercent,
  realTelemetry,
}: {
  state: EstimateState;
  selectedProvider?: TargetProvider;
  onSelectProvider?: (p: TargetProvider) => void;
  traffic?: TrafficInputs;
  onChangeRps?: (rps: number) => void;
  onChangeIdlePercent?: (idle: number) => void;
  realTelemetry?: RealRunTelemetry;
}) {
  const liveData = state.status === "ready" ? state.data : null;

  if (state.status === "error") {
    return (
      <p role="alert" className="mt-2 text-xs text-rose-700">
        {state.message}
      </p>
    );
  }

  if (!liveData) {
    return <p className="mt-2 text-xs text-ink/60">가격 API 에서 단가를 받는 중입니다…</p>;
  }

  const { input, report, arbitrage } = liveData;
  const savingOf = (cloud: string) => report.savings.find((s) => s.cloud === cloud);
  const sourceTotal = report.savings[0]?.current_monthly_usd ?? null;

  const isSourceSelected = selectedProvider === input.current.cloud;
  const selectedCost: CloudCost | null = isSourceSelected
    ? input.current
    : (input.candidates.find((c) => c.cloud === selectedProvider) ?? null);
  const selectedSaving = isSourceSelected ? undefined : savingOf(selectedProvider);
  const selectedTotal = selectedCost === null ? null : lineTotal(selectedSaving, sourceTotal);

  return (
    <div className="mt-3">
      {/* 핵심 상단 영역 (접히지 않는 1단: 좌측 핵심 수치 요약 + 우측 실시간 비용 교차 그래프) */}
      <div className="grid gap-3 lg:grid-cols-2">
        {/* 좌측: 선택 목적지 견적 + 동적 차익거래 + 실제 런 복제지연 연동 */}
        <div className="flex flex-col justify-between rounded-lg border border-ink/20 bg-white p-3.5">
          <div>
            <div className="flex items-baseline justify-between gap-2 border-b border-ink/10 pb-2">
              <div>
                <span className="font-mono text-xs font-bold text-ink">
                  [{cloudLabel(selectedProvider)}] {selectedCost?.region ?? NOT_PROVIDED}
                </span>
                <p className="mt-0.5 text-[11px] font-semibold text-indigo-900">
                  {selectedCost === null
                    ? "이 클라우드는 비교 후보에 없습니다"
                    : isSourceSelected
                      ? `현재 원본 ${cloudLabel(input.current.cloud)} 기준 (${usd(sourceTotal)}/월)`
                      : savingText(selectedSaving)}
                </p>
              </div>
              <div className="text-right">
                <span className="font-mono text-xl font-bold tabular-nums text-ink">{usd(selectedTotal)}/월</span>
                <p className="text-[9px] text-amber-800">{ESTIMATE_LABEL} · 공시가 기준, 청구액 아님</p>
              </div>
            </div>

            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px]">
              {(selectedCost?.lines ?? []).map((line) => (
                <span key={line.item} title={line.source} className="text-ink/70">
                  {line.item.replace(" 컴퓨팅", "")}:{" "}
                  <strong className="text-ink">{usd(line.monthly_usd)}</strong>
                </span>
              ))}
            </div>
          </div>

          {/* 동적 차익거래 & 실제 run 복제 지연 1줄 요약 */}
          {arbitrage && (
            <div className="mt-3 rounded border border-ink/15 bg-paper/60 px-2.5 py-2 font-mono text-[11px]">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold text-ink">
                  동적 차익거래 ({arbitrageStatus(arbitrage)})
                  <span className="ml-1 text-[9px] font-normal text-amber-800">{ESTIMATE_LABEL}</span>
                </span>
                <span className="font-bold text-indigo-900">
                  {arbitrage.monthly_savings !== null ? `월 ${usd(arbitrage.monthly_savings)} 절감` : NOT_PROVIDED}
                </span>
              </div>
              <div className="mt-1 flex flex-wrap items-center justify-between gap-2 text-[10px] text-ink/65">
                <span>
                  분산 비용 {usd(arbitrage.arbitrage_monthly_cost)} · egress 차이{" "}
                  {signedUsd(arbitrage.cross_cloud_egress_monthly_usd)}/월
                </span>
                {realTelemetry && (
                  <span>
                    실측 복제지연:{" "}
                    <strong>
                      {realTelemetry.replicationLagBytes === null
                        ? NOT_PROVIDED
                        : `${realTelemetry.replicationLagBytes.toLocaleString("en-US")} B`}
                    </strong>{" "}
                    · 진행률 {realTelemetry.progress}% ({realTelemetry.elapsedText})
                  </span>
                )}
              </div>
            </div>
          )}
        </div>

        {/* 우측: 한눈에 보이는 컴팩트 실시간 비용 교차 그래프 */}
        <CompactCrossoverChart
          x={input.crossover}
          arb={arbitrage}
          traffic={traffic}
          onChangeRps={onChangeRps}
          onChangeIdlePercent={onChangeIdlePercent}
        />
      </div>

      {/* 상세 비교표 및 근거 (평소에는 접혀 있음) */}
      <CollapsibleDetailedSection
        data={liveData}
        selectedProvider={selectedProvider}
        onSelectProvider={onSelectProvider}
      />
    </div>
  );
}
