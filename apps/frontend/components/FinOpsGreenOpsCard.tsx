"use client";

import { useEffect, useMemo, useState } from "react";
import { DbBasisRows } from "@/components/finops/DbBasisRows";
import { PriceApiEstimate, deriveLiveView, type RealRunTelemetry } from "@/components/finops/PriceApiEstimate";
import { SensitivityPanel } from "@/components/finops/SensitivityPanel";
import {
   METRIC_KEYS,
  elapsedSeconds,
  formatDuration,
  isFinished,
  numberMetric,
  overallProgress,
  servingSide,
} from "@/lib/dashboard";
import {
  CARBON_DATA_YEAR,
  DB_CLASSES,
  EXCLUDED_COSTS,
  FINOPS_DEFAULTS,
  NOT_PROVIDED,
  PRICE_AS_OF,
  PRICE_SOURCES,
  RIGHTSIZING_NOT_PROVIDED,
  SIMILAR_COST_BAND_PERCENT,
  SLIDER_RANGES,
  VERDICT_LABEL,
  buildCostReportText,
  computeBothBases,
  formatUsd,
  signedPercent,
  signedUsd,
  unsupportedTargetMessage,
  type CostReportInput,
  type CostVerdict,
  type DbClass,
  type FinOpsComparisonResult,
  type FinOpsInputParams,
  type InputOrigin,
  type ProviderFinOpsProfile,
  type TargetProvider,
} from "@/lib/finops";
import { estimateRequest, useFinopsEstimate, type FinopsEstimate } from "@/lib/finopsApi";
import { STAGE_LABEL } from "@/lib/labels";
import { isRecord } from "@/lib/plan";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
}

// 금액이 줄어도 조건이 붙은 추정이라 긍정 색을 쓰지 않습니다.
const VERDICT_TONE: Record<CostVerdict, string> = {
  COST_LOWER: "text-ink",
  COST_LOWER_CONDITIONAL: "text-ink",
  SIMILAR: "text-ink",
  COST_HIGHER: "text-rose-700",
};

const DISK_RANGE = { min: 10, max: 200, step: 10 };

const positiveNumber = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;

const nonEmptyString = (value: unknown): string | null =>
  typeof value === "string" && value.trim() !== "" ? value.trim() : null;

/** 계획에 있는 앱·DB 크기. 없으면 null 이고 화면에 "가정"으로 표시합니다. */
function readPlanSizing(run: RunStatus) {
  const planning = run.outputs.planning;
  const plan = isRecord(planning) && isRecord(planning.plan) ? planning.plan : null;
  const infra = plan && isRecord(plan.target_infra) ? plan.target_infra : null;
  return {
    cpuCount: positiveNumber(infra?.cpu),
    memoryMb: positiveNumber(infra?.memory),
    dbSizeGb: positiveNumber(infra?.db_disk_size_gb),
    dbTier: nonEmptyString(infra?.db_tier),
  };
}

function diskOriginOf(dbSizeOverride: number | null, planDiskGb: number | null): InputOrigin {
  if (dbSizeOverride !== null) return "사용자 입력";
  return planDiskGb !== null ? "계획값" : "가정";
}

function useFinOpsInputs(run: RunStatus) {
  const sizing = readPlanSizing(run);
  const cpuCount = sizing.cpuCount ?? FINOPS_DEFAULTS.cpuCount;
  const memoryMb = sizing.memoryMb ?? FINOPS_DEFAULTS.memoryMb;
  const planDbTier = sizing.dbTier;

  const reqProvider = run.request.target_infra?.provider;
  const initialProvider: TargetProvider =
    reqProvider === "azure" ? "azure" : reqProvider === "aws" ? "aws" : "gcp";
  const [targetProvider, setTargetProvider] = useState<TargetProvider>(initialProvider);
  const [rps, setRps] = useState<number>(FINOPS_DEFAULTS.rps);
  const [idlePercent, setIdlePercent] = useState<number>(FINOPS_DEFAULTS.idlePercent);
  const [avgRequestMs, setAvgRequestMs] = useState<number>(FINOPS_DEFAULTS.avgRequestMs);
  const [dbSizeOverride, setDbSizeOverride] = useState<number | null>(null);
  const [dbClass, setDbClass] = useState<DbClass>(FINOPS_DEFAULTS.dbClass);
  const dbSizeGb = dbSizeOverride ?? sizing.dbSizeGb ?? FINOPS_DEFAULTS.dbSizeGb;
  const origins: CostReportInput["origins"] = {
    targetApp: sizing.cpuCount !== null && sizing.memoryMb !== null ? "계획값" : "가정",
    disk: diskOriginOf(dbSizeOverride, sizing.dbSizeGb),
  };

  // 원본 앱 크기는 알 수 없어 목적지 크기를 그대로 씁니다. 화면에는 "가정"으로 표시합니다.
  const params = useMemo<FinOpsInputParams>(
    () => ({
      rps,
      idlePercent,
      avgRequestMs,
      dbSizeGb,
      dbClass,
      cpuCount,
      memoryMb,
      sourceCpuCount: cpuCount,
      sourceMemoryMb: memoryMb,
      targetProvider,
      dbBasis: "same_memory",
      planDbTier,
    }),
    [rps, idlePercent, avgRequestMs, dbSizeGb, dbClass, cpuCount, memoryMb, targetProvider, planDbTier],
  );
  const setters = { setTargetProvider, setRps, setIdlePercent, setAvgRequestMs, setDbSizeOverride, setDbClass };
  return { params, origins, setters };
}

type InputSetters = ReturnType<typeof useFinOpsInputs>["setters"];

function CarbonRow({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="flex justify-between gap-3">
      <span>{label}</span>
      <span className="text-right">{value ?? NOT_PROVIDED}</span>
    </div>
  );
}

function CostPanel({ title, profile }: { title: string; profile: ProviderFinOpsProfile }) {
  const { cost, carbon } = profile;
  const carbonSource = `(Google ${CARBON_DATA_YEAR})`;
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
        <CarbonRow
          label="전력망 탄소 집약도"
          value={carbon.gridIntensity === null ? null : `${carbon.gridIntensity} gCO2eq/kWh ${carbonSource}`}
        />
        <CarbonRow
          label="무탄소 에너지(CFE) 비율"
          value={carbon.cfePercent === null ? null : `${carbon.cfePercent}% ${carbonSource}`}
        />
      </div>
    </div>
  );
}

type CopyState = "idle" | "copied" | "failed";

function CopyReportButton({ input }: { input: CostReportInput }) {
  const [copyState, setCopyState] = useState<CopyState>("idle");

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(buildCostReportText(input));
      setCopyState("copied");
      setTimeout(() => setCopyState("idle"), 2500);
    } catch {
      setCopyState("failed");
    }
  };

  return (
    <>
      <button
        type="button"
        onClick={handleCopy}
        className="inline-flex items-center gap-1.5 rounded-lg border border-ink bg-paper px-3 py-1.5 text-xs font-medium text-ink shadow-[2px_2px_0_0_var(--color-ink)] transition-transform active:translate-x-0.5 active:translate-y-0.5 active:shadow-none"
      >
        {copyState === "copied" ? "✓ 복사 완료!" : "📋 참고 계산 복사"}
      </button>
      {copyState === "failed" && (
        <p role="alert" className="w-full text-right text-[11px] font-medium text-rose-700">
          복사하지 못했습니다. 브라우저 권한을 확인하세요.
        </p>
      )}
    </>
  );
}

const PROVIDER_SHORT_LABEL: Record<TargetProvider, string> = {
  gcp: "GCP",
  azure: "Azure",
  aws: "AWS",
};

const PROVIDERS: readonly TargetProvider[] = ["gcp", "azure", "aws"];

/**
 * 상단 단일 클라우드 선택 및 순위 탭 (중복 없이 이곳 1곳에만 표시)
 * 백엔드 가격 API 금액이 있으면 저렴한 순으로 순위와 금액을 버튼 안에 함께 표시합니다.
 */
function UnifiedCloudSelector({
  targetProvider,
  onSelect,
  estimateData,
}: {
  targetProvider: TargetProvider;
  onSelect: (p: TargetProvider) => void;
  estimateData: FinopsEstimate | null;
}) {
  const totals: Record<TargetProvider, number | null> = { gcp: null, azure: null, aws: null };
  if (estimateData) {
    totals.aws = estimateData.report.savings[0]?.current_monthly_usd ?? null;
    for (const s of estimateData.report.savings) {
      if ((s.cloud === "gcp" || s.cloud === "azure") && s.complete) {
        totals[s.cloud] = s.candidate_monthly_usd;
      }
    }
  }

  const valid = PROVIDERS.filter((p) => totals[p] !== null).sort(
    (a, b) => (totals[a] as number) - (totals[b] as number),
  );
  const ranks: Partial<Record<TargetProvider, number>> = {};
  valid.forEach((p, idx) => {
    ranks[p] = idx + 1;
  });

  // 순위가 정해졌으면 1위·2위·3위 순서대로 탭을 정렬합니다.
  const orderedProviders = [...PROVIDERS].sort((a, b) => (ranks[a] ?? 99) - (ranks[b] ?? 99));

  return (
    <div className="inline-flex flex-wrap rounded-lg border border-ink/20 bg-paper p-0.5 text-xs font-mono">
      {orderedProviders.map((p) => {
        const isActive = targetProvider === p;
        const rank = ranks[p];
        const total = totals[p];
        return (
          <button
            key={p}
            type="button"
            aria-pressed={isActive}
            onClick={() => onSelect(p)}
            className={`inline-flex items-center gap-1 rounded px-2.5 py-1 transition-colors ${
              isActive
                ? "border border-ink bg-white font-bold text-ink shadow-[1px_1px_0_0_var(--color-ink)]"
                : "font-medium text-ink/65 hover:text-ink"
            }`}
          >
            {rank !== undefined && <span className="text-[10px] text-ink/55">{rank}위</span>}
            <span>{PROVIDER_SHORT_LABEL[p]}</span>
            {total !== null && <span className="tabular-nums">{formatUsd(total)}</span>}
          </button>
        );
      })}
    </div>
  );
}

function CardHeader({
  report,
  setters,
  estimateData,
}: {
  report: CostReportInput;
  setters: InputSetters;
  estimateData: FinopsEstimate | null;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-ink/15 pb-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 id="finops-heading" className="text-base font-semibold sm:text-lg">
          멀티클라우드 월 비용 추정
        </h2>
        <span className="rounded border border-ink/30 bg-paper px-1.5 py-0.5 font-mono text-[10px] font-medium text-ink/75">
          공시가 기준 · 확인일 {PRICE_AS_OF}
        </span>
      </div>

      <div className="flex flex-wrap items-center justify-end gap-2">
        <UnifiedCloudSelector
          targetProvider={report.params.targetProvider}
          onSelect={setters.setTargetProvider}
          estimateData={estimateData}
        />
        <CopyReportButton input={report} />
      </div>
    </div>
  );
}

function HeroStats({ metrics, params }: { metrics: FinOpsComparisonResult; params: FinOpsInputParams }) {
  const tileClass = "rounded-lg border border-ink/15 bg-paper/60 p-3";
  return (
    <>
      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className={tileClass}>
          <p className="text-[11px] font-medium text-ink/60">월 비용 차이 (AWS 대비, 같은 메모리 등급)</p>
          <p className={`mt-1 font-mono text-2xl font-bold tabular-nums ${VERDICT_TONE[metrics.verdict]}`}>
            {signedPercent(metrics.deltaPercent)}
          </p>
          <p className="mt-0.5 font-mono text-[11px] text-ink/50">월 {signedUsd(metrics.monthlyDeltaUsd)}</p>
        </div>

        <div className={tileClass}>
          <p className="text-[11px] font-medium text-ink/60">연간 비용 차이</p>
          <p className="mt-1 font-mono text-2xl font-bold text-ink tabular-nums">{signedUsd(metrics.annualDeltaUsd)}</p>
          <p className="mt-0.5 font-mono text-[11px] text-ink/50">이전 1회 비용 미포함</p>
        </div>

        <div className={tileClass}>
          <p className="text-[11px] font-medium text-ink/60">탄소 감축량</p>
          <p className="mt-1 font-mono text-2xl font-bold text-ink/50">{NOT_PROVIDED}</p>
          <p className="mt-0.5 text-[11px] text-ink/50">전력 사용량 측정값이 없어 계산하지 않습니다</p>
        </div>

        <div className={tileClass}>
          <p className="text-[11px] font-medium text-ink/60">비용 판정 (기준 ±{SIMILAR_COST_BAND_PERCENT}%)</p>
          <p className={`mt-1 font-mono text-base font-bold ${VERDICT_TONE[metrics.verdict]}`}>
            {VERDICT_LABEL[metrics.verdict]}
          </p>
          <p className="mt-0.5 text-[11px] text-ink/60">{metrics.verdictReason}</p>
        </div>
      </div>
      <p className="mt-2 font-mono text-[11px] text-ink/60">
        {params.rps} RPS · 유휴 {params.idlePercent}% · {params.avgRequestMs}ms · 원본 {params.dbClass}
      </p>
    </>
  );
}

interface RangeFieldProps {
  label: string;
  valueText: string;
  value: number;
  range: { min: number; max: number; step: number };
  onChange: (value: number) => void;
}

function RangeField({ label, valueText, value, range, onChange }: RangeFieldProps) {
  return (
    <label className="block">
      <span className="flex justify-between text-[11px] font-mono">
        <span className="text-ink/65">{label}</span>
        <span className="font-semibold text-ink">{valueText}</span>
      </span>
      <input
        type="range"
        min={range.min}
        max={range.max}
        step={range.step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-1 h-1.5 w-full cursor-pointer accent-ink"
      />
    </label>
  );
}

function DbClassField({ params, setDbClass }: { params: FinOpsInputParams; setDbClass: InputSetters["setDbClass"] }) {
  return (
    <label className="block">
      <span className="text-[11px] font-mono text-ink/65">원본 DB 등급 (가정)</span>
      <select
        value={params.dbClass}
        onChange={(e) => setDbClass(e.target.value as DbClass)}
        className="mt-1 w-full rounded border border-ink/30 bg-white px-2 py-0.5 font-mono text-xs"
      >
        {(Object.keys(DB_CLASSES) as DbClass[]).map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
    </label>
  );
}

/**
 * 하단 슬라이더 바: 상단 핵심 요약·그래프 바로 아래에 배치하여 조작 결과를 위쪽에서 즉시 확인합니다.
 */
function AssumptionControls({ report, setters }: { report: CostReportInput; setters: InputSetters }) {
  const { params, origins } = report;
  return (
    <div className="mt-3 rounded-lg border border-ink/15 bg-paper/70 px-3.5 py-2.5">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <RangeField label="활성 평균 트래픽" valueText={`${params.rps} RPS`} value={params.rps} range={SLIDER_RANGES.rps} onChange={setters.setRps} />
        <RangeField label="유휴 시간 비율" valueText={`${params.idlePercent}%`} value={params.idlePercent} range={SLIDER_RANGES.idlePercent} onChange={setters.setIdlePercent} />
        <RangeField label="요청당 처리 시간" valueText={`${params.avgRequestMs} ms`} value={params.avgRequestMs} range={SLIDER_RANGES.avgRequestMs} onChange={setters.setAvgRequestMs} />
        <RangeField label={`DB 디스크 (${origins.disk})`} valueText={`${params.dbSizeGb} GB`} value={params.dbSizeGb} range={DISK_RANGE} onChange={setters.setDbSizeOverride} />
        <DbClassField params={params} setDbClass={setters.setDbClass} />
      </div>
    </div>
  );
}

function EvidenceDetails() {
  return (
    <details className="mt-4 rounded-lg border border-ink/15 p-3 text-xs text-ink/70">
      <summary className="cursor-pointer font-semibold text-ink">가격 출처와 계산에 넣지 않은 항목</summary>
      <div className="mt-3 grid gap-4 md:grid-cols-2">
        <div>
          <p className="font-semibold text-ink">공시가 출처 (확인일 {PRICE_AS_OF})</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4">
            {PRICE_SOURCES.map((source) => (
              <li key={source.url}>
                <a href={source.url} target="_blank" rel="noopener noreferrer" className="underline underline-offset-2">
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
            <li>요청당 응답 12KB, AWS·GCP 인스턴스 1개, Azure 최소 복제본 2개 기준입니다.</li>
            <li>
              참고 계산은 Cloud Run 을 요청 기반 과금으로 셉니다. 가격 API 견적은 Terraform 설정대로 인스턴스 기반 과금
              (요청 없는 시간 0대)으로 셉니다. 두 값이 다른 주된 이유입니다.
            </li>
            <li>탄소 값은 Google이 공개한 {CARBON_DATA_YEAR}년 서울 리전 값입니다. AWS·Azure는 같은 형식의 공개값을 확인하지 못했습니다.</li>
          </ul>
        </div>
      </div>
    </details>
  );
}

function FinOpsCardBody({ run }: Props) {
  const { params, origins, setters } = useFinOpsInputs(run);
  const bases = useMemo(() => computeBothBases(params), [params]);
  const metrics = bases.sameMemory;
  const report: CostReportInput = { params, origins };

  // 실제 run 이 실행 중일 때만 경과 시간을 1초마다 갱신합니다.
  const [nowMs, setNowMs] = useState(() => Date.now());
  const running = !isFinished(run.state);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);

  const realLag = numberMetric(run, METRIC_KEYS.replicationLagBytes);
  const estimate = useFinopsEstimate(estimateRequest(params, run.request.aws_region ?? undefined, realLag));
  const liveEstimateData = useMemo(
    () => (estimate.data ? deriveLiveView(estimate.data, params, realLag) : null),
    [estimate.data, params, realLag],
  );

  const realTelemetry: RealRunTelemetry = {
    progress: overallProgress(run),
    elapsedText: formatDuration(elapsedSeconds(run, nowMs)),
    stageLabel: run.current_stage ? STAGE_LABEL[run.current_stage] : isFinished(run.state) ? "종료" : "—",
    replicationLagBytes: realLag,
    servingSide: servingSide(run),
  };

  return (
    <section
      aria-labelledby="finops-heading"
      className="rounded-xl border border-ink bg-white p-4 shadow-[3px_3px_0_0_var(--color-ink)]"
    >
      {/* 1. 상단 헤더 & 단일 클라우드 선택/순위 탭 (중복 없음) */}
      <CardHeader report={report} setters={setters} estimateData={liveEstimateData} />

      {/* 2. 상단 핵심 요약 + 실시간 비용 교차 그래프 (항상 한눈에 보임) */}
      <PriceApiEstimate
        state={estimate}
        selectedProvider={params.targetProvider}
        onSelectProvider={setters.setTargetProvider}
        params={params}
        onChangeRps={setters.setRps}
        onChangeIdlePercent={setters.setIdlePercent}
        realTelemetry={realTelemetry}
      />

      {/* 3. 하단 조작 슬라이더 (핵심 영역 아래에 배치) */}
      <AssumptionControls report={report} setters={setters} />

      {/* 4. 참고 계산 및 민감도·탄소 집약도 (평소에는 접혀 있음) */}
      <details className="mt-3 rounded-lg border border-ink/15 bg-paper/30 p-3 text-xs">
        <summary className="cursor-pointer font-semibold text-ink/80 hover:text-ink">
          참고 계산 · DB 등급별 비교 · 민감도 테이블 · 그린옵스 펼치기
        </summary>
        <div className="mt-3">
          <p className="text-[11px] text-ink/60">
            위 가격 API 견적이 기준입니다. 아래는 화면 고정 단가 기준 DB 등급 비교 및 민감도 참고값입니다.
          </p>
          <HeroStats metrics={metrics} params={params} />
          <DbBasisRows bases={bases} planDbTier={params.planDbTier} />
          {params.targetProvider === "aws" && (
            <p className="mt-2 text-xs text-ink/70">AWS 목적지는 ECS Fargate + RDS 공시가 기준으로 비교합니다.</p>
          )}

          <div className="mt-4 grid gap-4 md:grid-cols-2">
            <CostPanel title={`[현재] AWS ${metrics.source.regionName}`} profile={metrics.source} />
            <CostPanel title={`[이전] ${metrics.target.providerName} ${metrics.target.regionName}`} profile={metrics.target} />
          </div>

          <SensitivityPanel params={params} />
          <p className="mt-3 text-xs font-medium text-ink/70">{RIGHTSIZING_NOT_PROVIDED}</p>
          <EvidenceDetails />
        </div>
      </details>
    </section>
  );
}

export function FinOpsGreenOpsCard({ run }: Props) {
  const unsupported = unsupportedTargetMessage(run.request.target_infra?.provider, run.request.target_region);
  if (unsupported) {
    return (
      <section aria-label="멀티클라우드 월 비용 추정" className="rounded-xl border border-ink/30 bg-paper/40 p-4 text-sm text-ink/70">
        {unsupported}
      </section>
    );
  }
  return <FinOpsCardBody run={run} />;
}
