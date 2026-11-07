"use client";

import { NOT_PROVIDED } from "@/lib/finops";
import type { ArbitrageResult, CloudCost, Crossover, EstimateState, FinopsEstimate, Saving } from "@/lib/finopsApi";

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

/** 상태 칩 하나. 복제 지연 차단이 먼저이고, 비중을 못 정했으면 "미제공" 입니다 (0% 로 쓰지 않음). */
function arbitrageStatus(arb: ArbitrageResult): string {
  if (arb.lag_fenced) return "복제 지연으로 원본 고정";
  if (arb.recommendation.startsWith(NOT_PROVIDED) || arb.read_weight_target === null || arb.read_weight_source === null) {
    return NOT_PROVIDED;
  }
  return `GCP ${Math.round(arb.read_weight_target * 100)}% · AWS ${Math.round(arb.read_weight_source * 100)}%`;
}

/** egress 단가는 소수 셋째 자리 이하도 있어 usd() 의 두 자리 반올림을 쓰지 않습니다. 순차이라 음수일 수 있습니다. */
const usdPerGb = (v: number) => `${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 4 })}/GB`;

/** egress 는 비용이 아니라 원본(AWS) 대비 순차이입니다. 음수면 목적지 송신이 더 쌉니다. */
const signedUsd = (v: number | null) => (v === null ? NOT_PROVIDED : `${v > 0 ? "+" : v < 0 ? "-" : ""}${usd(Math.abs(v))}`);

// 금액은 백엔드가 낸 USD 그대로입니다. 화면에서 환율을 곱하지 않습니다. 배치는 CrossoverTile 과 같습니다.
function ArbitrageTile({ arb }: { arb: ArbitrageResult }) {
  const savings =
    arb.monthly_savings === null
      ? NOT_PROVIDED
      : `${usd(arb.monthly_savings)}${arb.savings_percent === null ? "" : ` (${arb.savings_percent}%)`}`;
  return (
    <div className="rounded-lg border border-ink/15 bg-paper/60 p-3 text-xs">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="font-semibold text-ink">읽기 분산 (차익거래)</p>
        <span className="rounded border border-ink/20 bg-white px-1.5 py-0.5 font-mono text-[10px] text-ink/70">
          {arbitrageStatus(arb)}
        </span>
      </div>
      <dl className="mt-1.5 grid gap-x-4 gap-y-1 font-mono text-[11px] sm:grid-cols-2">
        <div className="flex justify-between gap-2">
          <dt className="text-ink/60">분산 적용 월 비용</dt>
          <dd>{usd(arb.arbitrage_monthly_cost)}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-ink/60">원본만 (Fargate)</dt>
          <dd>{usd(arb.baseline_monthly_cost)}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-ink/60">순절감</dt>
          <dd>{savings}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-ink/60">egress 차이 (원본 대비)</dt>
          <dd>{signedUsd(arb.cross_cloud_egress_monthly_usd)}/월</dd>
        </div>
      </dl>
      <p className="mt-1.5 text-[11px] text-ink/80">{arb.recommendation}</p>
      <details className="mt-1.5 text-[11px] text-ink/70">
        <summary className="cursor-pointer font-semibold text-ink">근거</summary>
        <ul className="mt-1 list-disc space-y-0.5 pl-4">
          <li>
            쓰기는 {arb.write_destination} 100% (액티브 마스터 {arb.active_master}).
          </li>
          <li>
            egress 단가{" "}
            {arb.egress_usd_per_gb === null
              ? NOT_PROVIDED
              : `${usdPerGb(arb.egress_usd_per_gb)} (출처: ${arb.egress_source ?? NOT_PROVIDED})`}
          </li>
          {arb.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      </details>
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
