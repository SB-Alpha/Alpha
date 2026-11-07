import { useEffect, useState } from "react";
import { DB_CLASSES, type FinOpsInputParams } from "@/lib/finops";
import { request } from "@/lib/workflows";

// POST /finops/estimate 계약 (apps/backend/app/features/finops/schemas.py). 숫자는 모두 백엔드 계산 코드가 냅니다.
// 금액을 모르면 null 이고 화면에 "미제공" 으로 씁니다. 0 으로 바꾸지 않습니다.

export interface CostLine {
  item: string;
  monthly_usd: number | null;
  source: string;
}

export interface CloudCost {
  cloud: string;
  region: string | null;
  lines: CostLine[];
}

export interface Saving {
  cloud: string;
  current_monthly_usd: number | null;
  candidate_monthly_usd: number | null;
  monthly_saving_usd: number | null;
  saving_pct: number | null;
  complete: boolean;
  missing: string[];
}

export interface Crossover {
  fargate_app_monthly_usd: number | null;
  cloud_run_instance_monthly_usd: number | null;
  break_even_idle_percent: number | null;
  break_even_rps: number | null;
  concurrency: number;
  max_instances: number;
  reason: string | null;
}

// 읽기 분산(차익거래) 결과. 비용 항목이 하나라도 미제공이면 비중·절감은 null 입니다.
// 화면은 통화를 따로 요청하지 않으므로 금액은 USD 입니다 (환율을 화면에서 정하지 않음).
export interface ArbitrageResult {
  read_weight_target: number | null;
  read_weight_source: number | null;
  write_destination: string;
  active_master: string;
  lag_fenced: boolean;
  baseline_monthly_cost: number | null;
  arbitrage_monthly_cost: number | null;
  monthly_savings: number | null;
  savings_percent: number | null;
  cross_cloud_egress_monthly_usd: number | null;
  egress_usd_per_gb: number | null;
  egress_source: string | null;
  currency: string;
  fx_rate: number | null;
  crossover_rps: number | null;
  recommendation: string;
  reasons: string[];
}

export interface FinopsEstimate {
  input: { current: CloudCost; candidates: CloudCost[]; assumptions: string[]; crossover: Crossover | null };
  report: { savings: Saving[]; best: string | null; summary: string[]; notes: string[]; summary_source: "rules" | "ai" };
  arbitrage?: ArbitrageResult | null;
}

/** 가격 API 로 비교할 후보. 단가를 받는 리전은 서울 두 곳입니다. */
export const ESTIMATE_CANDIDATES = [
  { cloud: "gcp", region: "asia-northeast3" },
  { cloud: "azure", region: "koreacentral" },
] as const;

// RDS db.t3 등급은 모두 vCPU 2개입니다. 메모리는 화면 선택값(DB_CLASSES)을 그대로 씁니다.
const DB_T3_VCPU = 2;
const SOURCE_REGION_FALLBACK = "ap-northeast-2";

/** 화면 입력값을 /finops/estimate 요청으로. 원본 앱 크기는 미제공이라 목적지 계획 크기를 씁니다 (화면에 "가정"). */
export function estimateRequest(
  params: FinOpsInputParams,
  sourceRegion: string | undefined,
  replicationLagBytes?: number | null,
) {
  const db = DB_CLASSES[params.dbClass];
  return {
    source_region: sourceRegion || SOURCE_REGION_FALLBACK,
    app_vcpu: params.sourceCpuCount,
    app_memory_mib: params.sourceMemoryMb,
    app_instances: 1,
    db_instance_class: params.dbClass,
    db_vcpu: DB_T3_VCPU,
    db_memory_gib: db.memoryGb,
    db_storage_gb: params.dbSizeGb,
    candidates: ESTIMATE_CANDIDATES,
    traffic: {
      idle_percent: params.idlePercent,
      rps: params.rps,
      avg_latency_ms: params.avgRequestMs,
      ...(typeof replicationLagBytes === "number" && Number.isFinite(replicationLagBytes) && replicationLagBytes >= 0
        ? { replication_lag_bytes: Math.round(replicationLagBytes) }
        : {}),
    },
  };
}

export type EstimateState =
  | { status: "loading"; data: FinopsEstimate | null }
  | { status: "ready"; data: FinopsEstimate }
  | { status: "error"; data: FinopsEstimate | null; message: string };

const DEBOUNCE_MS = 300;

/**
 * 입력이 바뀌면 300ms 기다렸다가 한 번만 부릅니다. 이전 요청은 취소해서 늦게 온 옛 응답이 화면을 덮지 않게 합니다.
 * 새 값을 받는 동안에는 직전 결과를 흐리게 보여 줍니다 (첫 호출은 가격표를 받느라 수십 초 걸릴 수 있습니다).
 */
export function useFinopsEstimate(body: ReturnType<typeof estimateRequest>): EstimateState {
  const key = JSON.stringify(body);
  // 마지막으로 받은 응답과 그 요청. 지금 입력(key)과 다르면 "조회 중" 입니다 (effect 안에서 상태를 바로 바꾸지 않음).
  const [last, setLast] = useState<{ key: string; data: FinopsEstimate | null; error: string | null } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      request<FinopsEstimate>("/api/finops/estimate", { method: "POST", body: key, signal: controller.signal })
        .then((data) => setLast({ key, data, error: null }))
        .catch((err: unknown) => {
          if (controller.signal.aborted) return;
          const message = err instanceof Error ? err.message : "가격 API 견적을 받지 못했습니다";
          setLast((prev) => ({ key, data: prev?.data ?? null, error: message }));
        });
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [key]);

  const data = last?.data ?? null;
  if (last === null || last.key !== key) return { status: "loading", data };
  if (last.error !== null) return { status: "error", data, message: last.error };
  return data ? { status: "ready", data } : { status: "loading", data: null };
}
