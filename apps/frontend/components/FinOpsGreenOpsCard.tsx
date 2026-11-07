"use client";

import { useMemo, useState } from "react";
import {
  CARBON_DATA_YEAR,
  DB_CLASSES,
  EXCLUDED_COSTS,
  FINOPS_DEFAULTS,
  PRICE_AS_OF,
  PRICE_SOURCES,
  SIMILAR_COST_BAND_PERCENT,
  computeFinOpsComparison,
  type CostVerdict,
  type DbClass,
  type ProviderFinOpsProfile,
  type TargetProvider,
} from "@/lib/finops";
import { isRecord } from "@/lib/plan";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
}

const NOT_PROVIDED = "미제공";

const VERDICT_LABEL: Record<CostVerdict, string> = {
  COST_LOWER: "비용 유리",
  SIMILAR: "비용 차이 작음",
  COST_HIGHER: "비용 불리",
};

const VERDICT_TONE: Record<CostVerdict, string> = {
  COST_LOWER: "text-emerald-700",
  SIMILAR: "text-ink",
  COST_HIGHER: "text-rose-700",
};

const positiveNumber = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;

/** 계획에 있는 앱·DB 크기. 없으면 null 이고 화면에 "가정"으로 표시합니다. */
function readPlanSizing(run: RunStatus) {
  const planning = run.outputs.planning;
  const plan = isRecord(planning) && isRecord(planning.plan) ? planning.plan : null;
  const infra = plan && isRecord(plan.target_infra) ? plan.target_infra : null;
  return {
    cpuCount: positiveNumber(infra?.cpu),
    memoryMb: positiveNumber(infra?.memory),
    dbSizeGb: positiveNumber(infra?.db_disk_size_gb),
  };
}

const formatUsd = (value: number): string =>
  `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const signedUsd = (value: number): string => `${value >= 0 ? "+" : "-"}${formatUsd(Math.abs(value))}`;
const signedPercent = (value: number): string => `${value >= 0 ? "+" : ""}${value}%`;

function CostPanel({ title, profile }: { title: string; profile: ProviderFinOpsProfile }) {
  const { cost, carbon } = profile;
  return (
    <div className="rounded-lg border border-ink/20 bg-white p-4">
      <div className="flex items-center justify-between gap-3 border-b border-ink/10 pb-2">
        <div>
          <span className="font-mono text-xs font-semibold text-ink">{title}</span>
          <p className="text-[11px] text-ink/60">{profile.services}</p>
        </div>
        <span className="shrink-0 whitespace-nowrap font-mono text-lg font-bold text-ink tabular-nums">
          {formatUsd(cost.total)}/월
        </span>
      </div>

      <div className="mt-3 space-y-1.5 font-mono text-xs">
        <div className="flex justify-between gap-3 text-ink/70">
          <span>컴퓨팅 ({profile.computeNote})</span>
          <span>{formatUsd(cost.compute)}</span>
        </div>
        <div className="flex justify-between gap-3 text-ink/70">
          <span>DB 인스턴스 & 스토리지</span>
          <span>{formatUsd(cost.database)}</span>
        </div>
        <div className="flex justify-between gap-3 text-ink/70">
          <span>네트워크 아웃바운드</span>
          <span>{formatUsd(cost.egress)}</span>
        </div>
      </div>

      <div className="mt-4 rounded border border-ink/10 bg-paper/40 p-2 font-mono text-[11px] text-ink/60">
        <div className="flex justify-between gap-3">
          <span>전력망 탄소 집약도</span>
          <span>{carbon.gridIntensity === null ? NOT_PROVIDED : `${carbon.gridIntensity} gCO2eq/kWh`}</span>
        </div>
        <div className="flex justify-between gap-3">
          <span>무탄소 에너지(CFE) 비율</span>
          <span>{carbon.cfePercent === null ? NOT_PROVIDED : `${carbon.cfePercent}%`}</span>
        </div>
      </div>
    </div>
  );
}

export function FinOpsGreenOpsCard({ run }: Props) {
  const sizing = readPlanSizing(run);
  const cpuCount = sizing.cpuCount ?? FINOPS_DEFAULTS.cpuCount;
  const memoryMb = sizing.memoryMb ?? FINOPS_DEFAULTS.memoryMb;
  const isAppSizeFromPlan = sizing.cpuCount !== null && sizing.memoryMb !== null;

  const initialProvider: TargetProvider = run.request.target_infra?.provider === "azure" ? "azure" : "gcp";
  const [targetProvider, setTargetProvider] = useState<TargetProvider>(initialProvider);
  const [rps, setRps] = useState<number>(FINOPS_DEFAULTS.rps);
  const [idlePercent, setIdlePercent] = useState<number>(FINOPS_DEFAULTS.idlePercent);
  const [avgRequestMs, setAvgRequestMs] = useState<number>(FINOPS_DEFAULTS.avgRequestMs);
  const [dbSizeGb, setDbSizeGb] = useState<number>(sizing.dbSizeGb ?? FINOPS_DEFAULTS.dbSizeGb);
  const [dbClass, setDbClass] = useState<DbClass>(FINOPS_DEFAULTS.dbClass);
  const [copied, setCopied] = useState<boolean>(false);

  const metrics = useMemo(
    () =>
      computeFinOpsComparison({
        rps,
        idlePercent,
        avgRequestMs,
        dbSizeGb,
        dbClass,
        cpuCount,
        memoryMb,
        targetProvider,
      }),
    [rps, idlePercent, avgRequestMs, dbSizeGb, dbClass, cpuCount, memoryMb, targetProvider],
  );

  const handleCopyReport = () => {
    const reportText = `[CloudMorph] 멀티클라우드 월 비용 추정 (공시가 기준, 실측 아님)
· 이전: ${metrics.source.providerName} ${metrics.source.regionName} → ${metrics.target.providerName} ${metrics.target.regionName}
· 월 비용: ${formatUsd(metrics.source.cost.total)} → ${formatUsd(metrics.target.cost.total)} (${signedUsd(metrics.monthlyDeltaUsd)}, ${signedPercent(metrics.deltaPercent)})
· 연간 차이: ${signedUsd(metrics.annualDeltaUsd)} (이전 1회 비용 미포함)
· 판정: ${VERDICT_LABEL[metrics.verdict]}. ${metrics.verdictReason}
· 입력: ${rps} RPS, 유휴 ${idlePercent}%, 요청당 ${avgRequestMs}ms, 앱 ${cpuCount} vCPU / ${memoryMb} MiB, ${dbClass}, 디스크 ${dbSizeGb}GB
· 공시가 확인일: ${PRICE_AS_OF}`;

    navigator.clipboard.writeText(reportText).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    });
  };

  return (
    <section
      aria-labelledby="finops-heading"
      className="rounded-xl border border-ink bg-white p-5 shadow-[3px_3px_0_0_var(--color-ink)]"
    >
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-ink/15 pb-4">
        <div>
          <span className="inline-flex items-center rounded border border-ink/40 bg-paper px-2 py-0.5 font-mono text-[11px] font-semibold text-ink">
            추정: 공시가 기준, 실측 아님
          </span>
          <h2 id="finops-heading" className="mt-1 text-lg font-semibold sm:text-xl">
            멀티클라우드 월 비용 추정
          </h2>
          <p className="mt-0.5 text-xs text-ink/65">
            서울 리전 공시가와 아래 입력값으로 계산합니다. 실제 청구액이 아닙니다. 공시가 확인일 {PRICE_AS_OF}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* Target Cloud Toggle */}
          <div className="inline-flex rounded-lg border border-ink/20 bg-paper p-0.5 text-xs font-mono">
            <button
              type="button"
              aria-pressed={targetProvider === "gcp"}
              onClick={() => setTargetProvider("gcp")}
              className={`rounded px-2.5 py-1 font-semibold transition-colors ${
                targetProvider === "gcp"
                  ? "border border-ink bg-white text-ink shadow-[1px_1px_0_0_var(--color-ink)]"
                  : "text-ink/60 hover:text-ink"
              }`}
            >
              GCP (Seoul)
            </button>
            <button
              type="button"
              aria-pressed={targetProvider === "azure"}
              onClick={() => setTargetProvider("azure")}
              className={`rounded px-2.5 py-1 font-semibold transition-colors ${
                targetProvider === "azure"
                  ? "border border-ink bg-white text-ink shadow-[1px_1px_0_0_var(--color-ink)]"
                  : "text-ink/60 hover:text-ink"
              }`}
            >
              Azure (Korea)
            </button>
          </div>

          <button
            type="button"
            onClick={handleCopyReport}
            className="inline-flex items-center gap-1.5 rounded-lg border border-ink bg-paper px-3 py-1.5 text-xs font-medium text-ink shadow-[2px_2px_0_0_var(--color-ink)] transition-transform active:translate-x-0.5 active:translate-y-0.5 active:shadow-none"
          >
            {copied ? "✓ 복사 완료!" : "📋 비용 추정 복사"}
          </button>
        </div>
      </div>

      {/* Hero Stats */}
      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded-lg border border-ink/15 bg-paper/60 p-3">
          <p className="text-[11px] font-medium text-ink/60">월 비용 차이 (AWS 대비)</p>
          <p className={`mt-1 font-mono text-2xl font-bold tabular-nums ${VERDICT_TONE[metrics.verdict]}`}>
            {signedPercent(metrics.deltaPercent)}
          </p>
          <p className="mt-0.5 font-mono text-[11px] text-ink/50">월 {signedUsd(metrics.monthlyDeltaUsd)}</p>
        </div>

        <div className="rounded-lg border border-ink/15 bg-paper/60 p-3">
          <p className="text-[11px] font-medium text-ink/60">연간 비용 차이</p>
          <p className="mt-1 font-mono text-2xl font-bold text-ink tabular-nums">
            {signedUsd(metrics.annualDeltaUsd)}
          </p>
          <p className="mt-0.5 font-mono text-[11px] text-ink/50">이전 1회 비용 미포함</p>
        </div>

        <div className="rounded-lg border border-ink/15 bg-paper/60 p-3">
          <p className="text-[11px] font-medium text-ink/60">탄소 감축량</p>
          <p className="mt-1 font-mono text-2xl font-bold text-ink/50">{NOT_PROVIDED}</p>
          <p className="mt-0.5 text-[11px] text-ink/50">전력 사용량 측정값이 없어 계산하지 않습니다</p>
        </div>

        <div className="rounded-lg border border-ink/15 bg-paper/60 p-3">
          <p className="text-[11px] font-medium text-ink/60">
            비용 판정 (기준 ±{SIMILAR_COST_BAND_PERCENT}%)
          </p>
          <p className={`mt-1 font-mono text-base font-bold ${VERDICT_TONE[metrics.verdict]}`}>
            {VERDICT_LABEL[metrics.verdict]}
          </p>
          <p className="mt-0.5 text-[11px] text-ink/60">{metrics.verdictReason}</p>
        </div>
      </div>

      {/* Comparison Grid */}
      <div className="mt-5 grid gap-4 md:grid-cols-2">
        <CostPanel title={`[현재] AWS ${metrics.source.regionName}`} profile={metrics.source} />
        <CostPanel
          title={`[이전] ${metrics.target.providerName} ${metrics.target.regionName}`}
          profile={metrics.target}
        />
      </div>

      {/* Assumption Controls */}
      <div className="mt-5 rounded-lg border border-ink/15 bg-paper p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs font-semibold text-ink">가정 입력 (실측 아님)</p>
          <span className="font-mono text-[11px] text-ink/50">
            앱 {cpuCount} vCPU / {memoryMb} MiB ({isAppSizeFromPlan ? "계획값" : "가정"})
          </span>
        </div>

        <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <label className="block">
            <span className="flex justify-between text-xs font-mono">
              <span className="text-ink/60">활성 시간 평균 트래픽</span>
              <span className="font-semibold">{rps} RPS</span>
            </span>
            <input
              type="range"
              min="1"
              max="100"
              value={rps}
              onChange={(e) => setRps(Number(e.target.value))}
              className="mt-1.5 w-full accent-ink"
            />
          </label>

          <label className="block">
            <span className="flex justify-between text-xs font-mono">
              <span className="text-ink/60">유휴 시간 비율 (야간/주말)</span>
              <span className="font-semibold">{idlePercent}%</span>
            </span>
            <input
              type="range"
              min="0"
              max="80"
              step="5"
              value={idlePercent}
              onChange={(e) => setIdlePercent(Number(e.target.value))}
              className="mt-1.5 w-full accent-ink"
            />
          </label>

          <label className="block">
            <span className="flex justify-between text-xs font-mono">
              <span className="text-ink/60">요청당 처리 시간</span>
              <span className="font-semibold">{avgRequestMs} ms</span>
            </span>
            <input
              type="range"
              min="20"
              max="1000"
              step="20"
              value={avgRequestMs}
              onChange={(e) => setAvgRequestMs(Number(e.target.value))}
              className="mt-1.5 w-full accent-ink"
            />
          </label>

          <label className="block">
            <span className="flex justify-between text-xs font-mono">
              <span className="text-ink/60">DB 디스크{sizing.dbSizeGb === null ? " (가정)" : ""}</span>
              <span className="font-semibold">{dbSizeGb} GB</span>
            </span>
            <input
              type="range"
              min="10"
              max="200"
              step="10"
              value={dbSizeGb}
              onChange={(e) => setDbSizeGb(Number(e.target.value))}
              className="mt-1.5 w-full accent-ink"
            />
          </label>

          <label className="block">
            <span className="text-xs font-mono text-ink/60">원본 DB 등급 (가정)</span>
            <select
              value={dbClass}
              onChange={(e) => setDbClass(e.target.value as DbClass)}
              className="mt-1 w-full rounded border border-ink/30 bg-white px-2 py-1 font-mono text-xs"
            >
              {(Object.keys(DB_CLASSES) as DbClass[]).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      {/* Evidence */}
      <details className="mt-4 rounded-lg border border-ink/15 p-3 text-xs text-ink/70">
        <summary className="cursor-pointer font-semibold text-ink">가격 출처와 계산에 넣지 않은 항목</summary>
        <div className="mt-3 grid gap-4 md:grid-cols-2">
          <div>
            <p className="font-semibold text-ink">공시가 출처 (확인일 {PRICE_AS_OF})</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-4">
              {PRICE_SOURCES.map((source) => (
                <li key={source.url}>
                  <a
                    href={source.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline underline-offset-2"
                  >
                    {source.label}
                  </a>
                </li>
              ))}
            </ul>
          </div>
          <div>
            <p className="font-semibold text-ink">계산에 넣지 않은 항목</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-4">
              {EXCLUDED_COSTS.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
            <p className="mt-2 font-semibold text-ink">계산 가정</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-4">
              <li>요청당 응답 12KB, 앱은 인스턴스 1개 기준입니다.</li>
              <li>탄소 값은 Google이 공개한 {CARBON_DATA_YEAR}년 서울 리전 값입니다. AWS·Azure는 같은 형식의 공개값을 확인하지 못했습니다.</li>
            </ul>
          </div>
        </div>
      </details>
    </section>
  );
}
