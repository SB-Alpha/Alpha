"use client";

import { useMemo, useState } from "react";
import { DbBasisRows } from "@/components/finops/DbBasisRows";
import { SensitivityPanel } from "@/components/finops/SensitivityPanel";
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

  const initialProvider: TargetProvider = run.request.target_infra?.provider === "azure" ? "azure" : "gcp";
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

  // clipboard 가 없는 환경의 TypeError 와 권한 거부를 함께 처리합니다.
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
        {copyState === "copied" ? "✓ 복사 완료!" : "📋 비용 추정 복사"}
      </button>
      {copyState === "failed" && (
        <p role="alert" className="w-full text-right text-[11px] font-medium text-rose-700">
          복사하지 못했습니다. 브라우저 권한을 확인하세요.
        </p>
      )}
    </>
  );
}

function ProviderButton({ label, isActive, onClick }: { label: string; isActive: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-pressed={isActive}
      onClick={onClick}
      className={`rounded px-2.5 py-1 font-semibold transition-colors ${
        isActive ? "border border-ink bg-white text-ink shadow-[1px_1px_0_0_var(--color-ink)]" : "text-ink/60 hover:text-ink"
      }`}
    >
      {label}
    </button>
  );
}

function CardHeader({ report, setters }: { report: CostReportInput; setters: InputSetters }) {
  const { targetProvider } = report.params;
  return (
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

      <div className="flex flex-wrap items-center justify-end gap-2">
        <div className="inline-flex rounded-lg border border-ink/20 bg-paper p-0.5 text-xs font-mono">
          <ProviderButton label="GCP (Seoul)" isActive={targetProvider === "gcp"} onClick={() => setters.setTargetProvider("gcp")} />
          <ProviderButton label="Azure (Korea)" isActive={targetProvider === "azure"} onClick={() => setters.setTargetProvider("azure")} />
        </div>
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
      <span className="flex justify-between text-xs font-mono">
        <span className="text-ink/60">{label}</span>
        <span className="font-semibold">{valueText}</span>
      </span>
      <input
        type="range"
        min={range.min}
        max={range.max}
        step={range.step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-1.5 w-full accent-ink"
      />
    </label>
  );
}

function DbClassField({ params, setDbClass }: { params: FinOpsInputParams; setDbClass: InputSetters["setDbClass"] }) {
  return (
    <label className="block">
      <span className="text-xs font-mono text-ink/60">원본 DB 등급 (가정)</span>
      <select
        value={params.dbClass}
        onChange={(e) => setDbClass(e.target.value as DbClass)}
        className="mt-1 w-full rounded border border-ink/30 bg-white px-2 py-1 font-mono text-xs"
      >
        {(Object.keys(DB_CLASSES) as DbClass[]).map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
      {params.planDbTier === "db-g1-small" && (
        <span className="mt-1 block text-[11px] text-ink/60">
          계획의 db_tier 가 db-g1-small 이면 원본은 1GiB 등급일 가능성이 큽니다. 아래 등급별 값을 함께 보세요.
        </span>
      )}
    </label>
  );
}

function AssumptionControls({ report, setters }: { report: CostReportInput; setters: InputSetters }) {
  const { params, origins } = report;
  return (
    <div className="mt-5 rounded-lg border border-ink/15 bg-paper p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-semibold text-ink">가정 입력 (실측 아님)</p>
        <div className="text-right font-mono text-[11px] text-ink/50">
          <p>원본 앱: 가정 (원본 크기 미제공, 목적지 계획 크기 재사용)</p>
          <p>
            목적지 앱 {params.cpuCount} vCPU / {params.memoryMb} MiB ({origins.targetApp})
          </p>
        </div>
      </div>

      <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <RangeField label="활성 시간 평균 트래픽" valueText={`${params.rps} RPS`} value={params.rps} range={SLIDER_RANGES.rps} onChange={setters.setRps} />
        <RangeField label="유휴 시간 비율 (야간/주말)" valueText={`${params.idlePercent}%`} value={params.idlePercent} range={SLIDER_RANGES.idlePercent} onChange={setters.setIdlePercent} />
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
            <li>Cloud Run 은 요청 기반 과금으로 가정했습니다. 실제 배포 설정은 확인이 필요합니다.</li>
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

  return (
    <section
      aria-labelledby="finops-heading"
      className="rounded-xl border border-ink bg-white p-5 shadow-[3px_3px_0_0_var(--color-ink)]"
    >
      <CardHeader report={report} setters={setters} />
      <HeroStats metrics={metrics} params={params} />
      <DbBasisRows bases={bases} planDbTier={params.planDbTier} />
      {params.targetProvider === "azure" && (
        <p className="mt-2 text-xs text-ink/70">Azure 는 이 브랜치에 배포 어댑터가 없어 공시가 비교만 합니다.</p>
      )}

      <div className="mt-5 grid gap-4 md:grid-cols-2">
        <CostPanel title={`[현재] AWS ${metrics.source.regionName}`} profile={metrics.source} />
        <CostPanel title={`[이전] ${metrics.target.providerName} ${metrics.target.regionName}`} profile={metrics.target} />
      </div>

      <AssumptionControls report={report} setters={setters} />
      <SensitivityPanel params={params} />
      <p className="mt-3 text-xs font-medium text-ink/70">{RIGHTSIZING_NOT_PROVIDED}</p>
      <EvidenceDetails />
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
