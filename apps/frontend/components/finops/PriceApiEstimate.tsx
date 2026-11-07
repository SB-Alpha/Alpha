"use client";

import { NOT_PROVIDED } from "@/lib/finops";
import type { ArbitrageResult, CloudCost, Crossover, EstimateState, FinopsEstimate, Saving } from "@/lib/finopsApi";
import { useState } from "react";

// 백엔드 /finops/estimate 결과를 그대로 그립니다. 이 파일은 계산하지 않습니다 (금액·절감률·교차점 모두 백엔드 값).

const CLOUD_LABEL: Record<string, string> = { aws: "AWS", gcp: "GCP", azure: "Azure" };

const usd = (v: number | null | undefined) =>
  v === null || v === undefined ? NOT_PROVIDED : `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

/** 합계는 백엔드 값만 씁니다. 후보 합계는 모든 항목이 있고 원본과 맞는 완전한 비교일 때만 보입니다. */
function lineTotal(saving: Saving | undefined, sourceTotal: number | null): number | null {
  if (saving === undefined) return sourceTotal;
  return saving.complete ? saving.candidate_monthly_usd : null;
}

function savingText(s: Saving | undefined): string {
  if (!s || s.monthly_saving_usd === null) return "비교 불가 (금액 미제공 항목 있음)";
  const pct = s.saving_pct === null ? "" : ` (${s.saving_pct > 0 ? "-" : "+"}${Math.abs(s.saving_pct)}%)`;
  return s.monthly_saving_usd >= 0 ? `월 ${usd(s.monthly_saving_usd)} 감소${pct}` : `월 ${usd(-s.monthly_saving_usd)} 증가${pct}`;
}

function CloudColumn({
  cost,
  saving,
  isBest,
  sourceTotal,
}: {
  cost: CloudCost;
  saving?: Saving;
  isBest: boolean;
  sourceTotal: number | null;
}) {
  const isSource = saving === undefined;
  return (
    <div className={`rounded-lg border p-3 ${isBest ? "border-ink shadow-[2px_2px_0_0_var(--color-ink)]" : "border-ink/20"} bg-white`}>
      <div className="flex items-baseline justify-between gap-2 border-b border-ink/10 pb-1.5">
        <span className="font-mono text-xs font-semibold">
          {isSource ? "[현재] " : ""}
          {CLOUD_LABEL[cost.cloud] ?? cost.cloud} <span className="font-normal text-ink/50">{cost.region}</span>
        </span>
        <span className="font-mono text-base font-bold tabular-nums">{usd(lineTotal(saving, sourceTotal))}/월</span>
      </div>
      <ul className="mt-2 space-y-1 font-mono text-[11px]">
        {cost.lines.map((line) => (
          <li key={line.item} className="flex justify-between gap-2" title={line.source}>
            <span className="text-ink/70">{line.item}</span>
            <span className={line.monthly_usd === null ? "text-ink/40" : ""}>{usd(line.monthly_usd)}</span>
          </li>
        ))}
      </ul>
      {!isSource && <p className="mt-2 text-[11px] font-medium text-ink/80">{savingText(saving)}</p>}
      {isBest && <p className="mt-0.5 text-[11px] text-ink/60">계산 코드가 고른 최대 절감 후보</p>}
    </div>
  );
}

function CrossoverTile({ x }: { x: Crossover }) {
  return (
    <div className="rounded-lg border border-ink/15 bg-paper/60 p-3 text-xs">
      <p className="font-semibold text-ink">Cloud Run ↔ Fargate 교차점 (앱 컴퓨팅만)</p>
      <dl className="mt-1.5 grid gap-x-4 gap-y-1 font-mono text-[11px] sm:grid-cols-2">
        <div className="flex justify-between gap-2">
          <dt className="text-ink/60">유휴 비율 기준</dt>
          <dd>{x.break_even_idle_percent === null ? NOT_PROVIDED : `${x.break_even_idle_percent}% 초과 시 Cloud Run 이 작음`}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-ink/60">RPS 기준 (지금 유휴 비율)</dt>
          <dd>{x.break_even_rps === null ? NOT_PROVIDED : `${x.break_even_rps} RPS 이하`}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-ink/60">Fargate 앱 (내내 켜짐)</dt>
          <dd>{usd(x.fargate_app_monthly_usd)}/월</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-ink/60">Cloud Run 1대 한 달 내내</dt>
          <dd>{usd(x.cloud_run_instance_monthly_usd)}/월</dd>
        </div>
      </dl>
      <p className="mt-1.5 text-[11px] text-ink/60">
        가정: 인스턴스당 동시 {x.concurrency}건, 최대 {x.max_instances}대 (Terraform).{x.reason ? ` ${x.reason}.` : ""}
      </p>
    </div>
  );
}

const FX_RATES: Record<string, { rate: number; symbol: string }> = {
  USD: { rate: 1.0, symbol: "$" },
  JPY: { rate: 154.5, symbol: "¥" },
  KRW: { rate: 1420.0, symbol: "₩" },
};

function ArbitrageTile({ arb }: { arb: ArbitrageResult }) {
  const [curr, setCurr] = useState<"USD" | "JPY" | "KRW">("USD");
  const { rate, symbol } = FX_RATES[curr];

  const formatCost = (usdVal: number) => {
    const val = usdVal * rate;
    if (curr === "JPY" || curr === "KRW") return `${symbol}${Math.round(val).toLocaleString()}`;
    return `${symbol}${val.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };

  return (
    <div className="rounded-lg border-2 border-emerald-600/30 bg-emerald-50/40 p-3.5 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-emerald-950">비용 변동성 대응 동적 차익거래 (Dynamic Routing)</span>
          <span className="rounded bg-emerald-200/80 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-emerald-900">
            소프트뱅크 아키텍처
          </span>
        </div>
        <div className="flex items-center gap-1 rounded border border-ink/20 bg-white p-0.5 text-[11px]">
          {(["USD", "JPY", "KRW"] as const).map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => setCurr(c)}
              className={`rounded px-1.5 py-0.5 font-mono text-[10px] transition-colors ${
                curr === c ? "bg-ink font-bold text-white" : "text-ink/60 hover:text-ink"
              }`}
            >
              {c}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-2.5 flex flex-wrap gap-2 text-[11px]">
        <div className="inline-flex items-center gap-1.5 rounded-md border border-ink/15 bg-white px-2 py-1 font-mono">
          <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" />
          <span className="font-semibold">쓰기 펜싱:</span>
          <span className="text-ink/80">Active Master (SOURCE 100%)</span>
        </div>
        <div
          className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 font-mono ${
            arb.lag_fenced
              ? "border-amber-300 bg-amber-50 text-amber-900"
              : "border-emerald-200 bg-emerald-100/50 text-emerald-900"
          }`}
        >
          <span className={`inline-block h-2 w-2 rounded-full ${arb.lag_fenced ? "bg-amber-500" : "bg-emerald-600"}`} />
          <span className="font-semibold">복제 세이프가드:</span>
          <span>{arb.lag_fenced ? "지연 초과로 차익거래 차단 (100% 원본 펜싱)" : "지연 임계치 세이프가드 (< 1KB 연동)"}</span>
        </div>
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-3">
        <div className="rounded border border-ink/10 bg-white p-2">
          <p className="font-mono text-[10px] text-ink/60">읽기 트래픽 분산 (Read Split)</p>
          <p className="mt-1 font-mono text-xs font-bold text-ink">
            GCP {Math.round(arb.read_weight_target * 100)}% · AWS {Math.round(arb.read_weight_source * 100)}%
          </p>
        </div>
        <div className="rounded border border-ink/10 bg-white p-2">
          <p className="font-mono text-[10px] text-ink/60">차익거래 적용 월 비용</p>
          <p className="mt-1 font-mono text-xs font-bold text-ink">
            {formatCost(arb.arbitrage_monthly_cost)}
            <span className="ml-1 text-[10px] font-normal text-ink/50">(기준 {formatCost(arb.baseline_monthly_cost)})</span>
          </p>
        </div>
        <div className="rounded border border-emerald-600/30 bg-emerald-100/30 p-2">
          <p className="font-mono text-[10px] text-emerald-800">예상 월 순절감액 (Egress 반영)</p>
          <p className="mt-1 font-mono text-xs font-bold text-emerald-800">
            {formatCost(arb.monthly_savings)} ({arb.savings_percent}%)
          </p>
        </div>
      </div>

      <p className="mt-2.5 text-[11px] font-medium text-emerald-950">{arb.recommendation}</p>

      <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-[10px] text-ink/70">
        {arb.reasons.map((r) => (
          <li key={r}>{r}</li>
        ))}
        {arb.cross_cloud_egress_monthly_usd > 0 && (
          <li>
            크로스 클라우드 네트워크 Egress 비용(월 {formatCost(arb.cross_cloud_egress_monthly_usd)}, $0.09/GB 기준)이 공제된
            순절감액입니다.
          </li>
        )}
      </ul>
    </div>
  );
}

function EstimateBody({ data }: { data: FinopsEstimate }) {
  const { input, report } = data;
  const savingOf = (cloud: string) => report.savings.find((s) => s.cloud === cloud);
  // 원본 합계는 후보별 절감 계산에 쓰인 값(모든 항목이 있을 때만 숫자)과 같습니다
  const sourceTotal = report.savings[0]?.current_monthly_usd ?? null;
  return (
    <>
      <div className="mt-3 grid gap-3 md:grid-cols-3">
        <CloudColumn cost={input.current} isBest={false} sourceTotal={sourceTotal} />
        {input.candidates.map((c) => (
          <CloudColumn
            key={c.cloud}
            cost={c}
            saving={savingOf(c.cloud) ?? undefined}
            isBest={report.best === c.cloud}
            sourceTotal={sourceTotal}
          />
        ))}
      </div>
      <ul className="mt-3 space-y-0.5 text-xs text-ink/80">
        {report.summary.map((s) => (
          <li key={s}>{s}</li>
        ))}
      </ul>
      {input.crossover && (
        <div className="mt-3">
          <CrossoverTile x={input.crossover} />
        </div>
      )}
      {data.arbitrage && (
        <div className="mt-3">
          <ArbitrageTile arb={data.arbitrage} />
        </div>
      )}
      <details className="mt-3 text-xs text-ink/70">
        <summary className="cursor-pointer font-semibold text-ink">
          주의점과 가정 {report.summary_source === "ai" ? "(끝부분은 AI 설명, 숫자 없음)" : "(계산 코드)"}
        </summary>
        <ul className="mt-1 list-disc space-y-0.5 pl-4">
          {report.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      </details>
    </>
  );
}

export function PriceApiEstimate({ state }: { state: EstimateState }) {
  const isStale = state.status === "loading" && state.data !== null;
  return (
    <section aria-labelledby="price-api-heading" aria-busy={state.status === "loading"} className="mt-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 id="price-api-heading" className="text-sm font-semibold">
          가격 API 견적 (AWS · GCP · Azure, 백엔드 계산)
        </h3>
        <span className="font-mono text-[11px] text-ink/60">
          {state.status === "loading" ? "가격 조회 중… (첫 조회는 수십 초)" : state.status === "error" ? "조회 실패" : "단가 출처는 항목에 마우스를 올리면 보입니다"}
        </span>
      </div>
      {state.status === "error" && (
        <p role="alert" className="mt-2 text-xs text-rose-700">
          {state.message}
        </p>
      )}
      {state.data ? (
        <div className={isStale ? "opacity-60 transition-opacity" : "transition-opacity"}>
          <EstimateBody data={state.data} />
        </div>
      ) : (
        state.status === "loading" && <p className="mt-2 text-xs text-ink/60">가격 API 에서 단가를 받는 중입니다.</p>
      )}
    </section>
  );
}
