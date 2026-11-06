"use client";

import { useMemo, useState } from "react";
import { computeFinOpsComparison } from "@/lib/finops";
import type { RunStatus } from "@/lib/workflows";

interface Props {
  run: RunStatus;
}

export function FinOpsGreenOpsCard({ run }: Props) {
  // 기본 설정: 요청된 대상 인프라가 azure이면 azure, 기본은 gcp
  const initialProvider = run.request.target_infra?.provider === "azure" ? "azure" : "gcp";
  const [targetProvider, setTargetProvider] = useState<"gcp" | "azure">(initialProvider);
  const [rps, setRps] = useState<number>(10);
  const [idlePercent, setIdlePercent] = useState<number>(40);
  const [dbSizeGb, setDbSizeGb] = useState<number>(50);
  const [copied, setCopied] = useState<boolean>(false);

  const metrics = useMemo(() => {
    return computeFinOpsComparison({
      rps,
      idlePercent,
      dbSizeGb,
      targetProvider,
    });
  }, [rps, idlePercent, dbSizeGb, targetProvider]);

  const handleCopyReport = () => {
    const reportText = `[CloudMorph Nexus] FinOps & GreenOps TCO 분석 보고서
· 이전 대상: ${metrics.source.providerName} (${metrics.source.regionName}) → ${metrics.target.providerName} (${metrics.target.regionName})
· 월간 TCO: $${metrics.source.cost.total} → $${metrics.target.cost.total} (-$${metrics.monthlySavingsUsd}/월, -${metrics.savingsPercent}%)
· 연간 절감액: $${metrics.annualSavingsUsd}/년 (손익분기 회수 기간: ${metrics.roiPaybackMonths}개월)
· 탄소 배출량: ${metrics.source.carbon.totalKgCO2e}kg → ${metrics.target.carbon.totalKgCO2e}kg CO2e (-${metrics.carbonReductionPercent}%, 소나무 ${metrics.equivalentTrees}그루 식수 효과)
· Itô-Kelly 자원 차익거래 지수: ${metrics.itoKellyScore} / 1.00 (${metrics.recommendation})
· 종합 권고: ${metrics.recommendationReason}`;

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
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center rounded border border-emerald-800 bg-emerald-50 px-2 py-0.5 font-mono text-[11px] font-semibold text-emerald-800">
              Nexus FinOps & GreenOps
            </span>
            <span className="font-mono text-xs text-ink/50">Itô-Kelly 최적화 지수 {metrics.itoKellyScore}</span>
          </div>
          <h2 id="finops-heading" className="mt-1 text-lg font-semibold sm:text-xl">
            멀티클라우드 TCO & 탄소 배출(ESG) 최적화 분석
          </h2>
          <p className="mt-0.5 text-xs text-ink/65">
            AWS 대비 이전 타깃의 Scale-to-Zero 유휴 절감 및 24/7 무탄소 에너지(CFE) 계량경제학 분석
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* Target Cloud Toggle */}
          <div className="inline-flex rounded-lg border border-ink/20 bg-paper p-0.5 text-xs font-mono">
            <button
              type="button"
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
            {copied ? "✓ 복사 완료!" : "📋 C-Level TCO 복사"}
          </button>
        </div>
      </div>

      {/* Hero Stats */}
      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded-lg border border-ink/15 bg-paper/60 p-3">
          <p className="text-[11px] font-medium text-ink/60">월간 TCO 절감율</p>
          <p className="mt-1 font-mono text-2xl font-bold text-emerald-700 tabular-nums">
            -{metrics.savingsPercent}%
          </p>
          <p className="mt-0.5 font-mono text-[11px] text-ink/50">월 -${metrics.monthlySavingsUsd}</p>
        </div>

        <div className="rounded-lg border border-ink/15 bg-paper/60 p-3">
          <p className="text-[11px] font-medium text-ink/60">연간 절감 예상액</p>
          <p className="mt-1 font-mono text-2xl font-bold text-ink tabular-nums">
            ${metrics.annualSavingsUsd.toLocaleString()}
          </p>
          <p className="mt-0.5 font-mono text-[11px] text-emerald-800">회수 {metrics.roiPaybackMonths}개월 만에 손익분기</p>
        </div>

        <div className="rounded-lg border border-ink/15 bg-paper/60 p-3">
          <p className="text-[11px] font-medium text-ink/60">탄소 배출 감축량 (ESG)</p>
          <p className="mt-1 font-mono text-2xl font-bold text-emerald-700 tabular-nums">
            -{metrics.carbonReductionPercent}%
          </p>
          <p className="mt-0.5 font-mono text-[11px] text-ink/50">소나무 {metrics.equivalentTrees}그루 식수 효과</p>
        </div>

        <div className="rounded-lg border border-ink/15 bg-paper/60 p-3">
          <p className="text-[11px] font-medium text-ink/60">자원 차익거래 추천</p>
          <p className="mt-1 font-mono text-base font-bold text-emerald-800">
            {metrics.recommendation === "STRONG_MIGRATE" ? "강력 이전 권고" : "이전 권고"}
          </p>
          <p className="mt-0.5 text-[11px] text-ink/60 line-clamp-1">{metrics.recommendationReason}</p>
        </div>
      </div>

      {/* Comparison Grid */}
      <div className="mt-5 grid gap-4 md:grid-cols-2">
        {/* Source Card */}
        <div className="rounded-lg border border-ink/20 bg-white p-4">
          <div className="flex items-center justify-between border-b border-ink/10 pb-2">
            <div>
              <span className="font-mono text-xs font-semibold text-rose-800">[현재] AWS 서울 (ap-northeast-2)</span>
              <p className="text-[11px] text-ink/60">ECS Fargate + RDS PostgreSQL (Always-On)</p>
            </div>
            <span className="font-mono text-lg font-bold text-ink tabular-nums">
              ${metrics.source.cost.total}/월
            </span>
          </div>

          <div className="mt-3 space-y-1.5 font-mono text-xs">
            <div className="flex justify-between text-ink/70">
              <span>컴퓨팅 (vCPU+RAM 고정점유)</span>
              <span>${metrics.source.cost.compute}</span>
            </div>
            <div className="flex justify-between text-ink/70">
              <span>DB 인스턴스 & 스토리지</span>
              <span>${metrics.source.cost.database}</span>
            </div>
            <div className="flex justify-between text-ink/70">
              <span>네트워크 아웃바운드 (Egress)</span>
              <span>${metrics.source.cost.egress}</span>
            </div>
          </div>

          <div className="mt-4 rounded border border-ink/10 bg-paper/40 p-2 font-mono text-[11px]">
            <div className="flex justify-between text-ink/60">
              <span>탄소 집약도</span>
              <span>{metrics.source.carbon.intensity} gCO2e/kWh</span>
            </div>
            <div className="flex justify-between text-ink/60">
              <span>월 탄소 배출량</span>
              <span>{metrics.source.carbon.totalKgCO2e} kg CO2e</span>
            </div>
            <div className="flex justify-between text-ink/60">
              <span>24/7 CFE 청정에너지 매칭</span>
              <span>{metrics.source.carbon.cfePercent}%</span>
            </div>
          </div>
        </div>

        {/* Target Card */}
        <div className="rounded-lg border-2 border-emerald-800 bg-emerald-50/20 p-4">
          <div className="flex items-center justify-between border-b border-emerald-800/20 pb-2">
            <div>
              <span className="font-mono text-xs font-semibold text-emerald-900">
                [이전] {metrics.target.providerName} 서울 ({metrics.target.region})
              </span>
              <p className="text-[11px] text-ink/60">Cloud Run (Scale-to-Zero) + Cloud SQL</p>
            </div>
            <span className="font-mono text-lg font-bold text-emerald-800 tabular-nums">
              ${metrics.target.cost.total}/월
            </span>
          </div>

          <div className="mt-3 space-y-1.5 font-mono text-xs">
            <div className="flex justify-between text-ink/70">
              <span>컴퓨팅 (유휴 {idlePercent}% 요금 $0)</span>
              <span className="text-emerald-800 font-semibold">${metrics.target.cost.compute}</span>
            </div>
            <div className="flex justify-between text-ink/70">
              <span>DB 인스턴스 & 스토리지</span>
              <span>${metrics.target.cost.database}</span>
            </div>
            <div className="flex justify-between text-ink/70">
              <span>네트워크 아웃바운드 (Cloudflare 캐시)</span>
              <span>${metrics.target.cost.egress}</span>
            </div>
          </div>

          <div className="mt-4 rounded border border-emerald-800/20 bg-emerald-100/30 p-2 font-mono text-[11px]">
            <div className="flex justify-between text-emerald-900">
              <span>탄소 집약도</span>
              <span className="font-semibold">{metrics.target.carbon.intensity} gCO2e/kWh</span>
            </div>
            <div className="flex justify-between text-emerald-900">
              <span>월 탄소 배출량</span>
              <span className="font-semibold">{metrics.target.carbon.totalKgCO2e} kg CO2e</span>
            </div>
            <div className="flex justify-between text-emerald-900">
              <span>24/7 CFE 청정에너지 매칭</span>
              <span className="font-semibold">{metrics.target.carbon.cfePercent}% (그리드 최상위)</span>
            </div>
          </div>
        </div>
      </div>

      {/* Interactive Simulation Controls */}
      <div className="mt-5 rounded-lg border border-ink/15 bg-paper p-4">
        <div className="flex items-center justify-between">
          <p className="text-xs font-semibold text-ink">실시간 워크로드 시뮬레이션 파라미터</p>
          <span className="font-mono text-[11px] text-ink/50">슬라이더를 조절하여 동적 절감액을 확인하세요</span>
        </div>

        <div className="mt-3 grid gap-4 sm:grid-cols-3">
          <div>
            <div className="flex justify-between text-xs font-mono">
              <span className="text-ink/60">평균 트래픽</span>
              <span className="font-semibold">{rps} RPS</span>
            </div>
            <input
              type="range"
              min="1"
              max="100"
              value={rps}
              onChange={(e) => setRps(Number(e.target.value))}
              className="mt-1.5 w-full accent-emerald-800"
            />
          </div>

          <div>
            <div className="flex justify-between text-xs font-mono">
              <span className="text-ink/60">유휴 시간 비율 (야간/주말)</span>
              <span className="font-semibold">{idlePercent}%</span>
            </div>
            <input
              type="range"
              min="0"
              max="80"
              step="5"
              value={idlePercent}
              onChange={(e) => setIdlePercent(Number(e.target.value))}
              className="mt-1.5 w-full accent-emerald-800"
            />
          </div>

          <div>
            <div className="flex justify-between text-xs font-mono">
              <span className="text-ink/60">DB 데이터 규모</span>
              <span className="font-semibold">{dbSizeGb} GB</span>
            </div>
            <input
              type="range"
              min="10"
              max="200"
              step="10"
              value={dbSizeGb}
              onChange={(e) => setDbSizeGb(Number(e.target.value))}
              className="mt-1.5 w-full accent-emerald-800"
            />
          </div>
        </div>
      </div>
    </section>
  );
}
