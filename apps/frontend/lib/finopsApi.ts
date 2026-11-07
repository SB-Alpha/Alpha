import { useEffect, useState } from "react";
import type { TargetProvider } from "@/lib/finops";
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

/** 트래픽 가정 (사용자 입력). RPS 와 유휴율을 둘 다 넣어야 트래픽 조건을 보냅니다. */
export interface TrafficInputs {
  rps: number | null;
  idlePercent: number | null;
  avgLatencyMs: number | null;
  /** 0~1 */
  readRatio: number | null;
  avgResponseKb: number | null;
}

/** 화면이 /finops/estimate 에 보내는 입력. 모르면 null 이고, 보내지 않아 백엔드가 "미제공" 으로 둡니다. */
export interface EstimateInputs {
  sourceCloud: TargetProvider | null;
  sourceRegion: string | null;
  /** 원본 앱 크기. assessment 결과에 없어 사용자가 넣을 때만 있습니다 (목적지 크기로 채우지 않음). */
  sourceAppVcpu: number | null;
  sourceAppMemoryMib: number | null;
  /** 후보에 띄울 앱 크기 (계획의 목적지 크기). */
  targetAppVcpu: number | null;
  targetAppMemoryMib: number | null;
  /** 원본 RDS 등급 (사용자 입력). 없으면 AWS DB 금액은 미제공. */
  dbInstanceClass: string | null;
  dbVcpu: number | null;
  dbMemoryGib: number | null;
  dbStorageGb: number | null;
  candidates: { cloud: string; region: string }[];
  traffic: TrafficInputs;
  replicationLagBytes: number | null;
}

const optionalField = <T>(key: string, value: T | null) => (value === null ? {} : { [key]: value });

/** 화면 입력값을 /finops/estimate 요청으로. 원본 위치를 모르면 null 이고 요청하지 않습니다. */
export function estimateRequest(inputs: EstimateInputs) {
  if (inputs.sourceCloud === null || inputs.sourceRegion === null) return null;
  const t = inputs.traffic;
  const lag = inputs.replicationLagBytes;
  const traffic =
    t.rps !== null && t.idlePercent !== null
      ? {
          rps: t.rps,
          idle_percent: t.idlePercent,
          ...optionalField("avg_latency_ms", t.avgLatencyMs),
          ...optionalField("read_ratio", t.readRatio),
          ...optionalField("avg_response_kb", t.avgResponseKb),
          ...(typeof lag === "number" && Number.isFinite(lag) && lag >= 0 ? { replication_lag_bytes: Math.round(lag) } : {}),
        }
      : null;
  return {
    source_cloud: inputs.sourceCloud,
    source_region: inputs.sourceRegion,
    ...optionalField("app_vcpu", inputs.sourceAppVcpu),
    ...optionalField("app_memory_mib", inputs.sourceAppMemoryMib),
    ...optionalField("target_app_vcpu", inputs.targetAppVcpu),
    ...optionalField("target_app_memory_mib", inputs.targetAppMemoryMib),
    ...optionalField("db_instance_class", inputs.dbInstanceClass),
    ...optionalField("db_vcpu", inputs.dbVcpu),
    ...optionalField("db_memory_gib", inputs.dbMemoryGib),
    ...optionalField("db_storage_gb", inputs.dbStorageGb),
    candidates: inputs.candidates,
    ...(traffic ? { traffic } : {}),
  };
}

export type EstimateState =
  | { status: "loading"; data: FinopsEstimate | null }
  | { status: "ready"; data: FinopsEstimate }
  | { status: "error"; data: FinopsEstimate | null; message: string };

const DEBOUNCE_MS = 300;

/**
 * 입력이 바뀌면 300ms 기다렸다가 한 번만 부릅니다. 이전 요청은 취소해서 늦게 온 옛 응답이 화면을 덮지 않게 합니다.
 * 새 값을 받는 동안에는 loading 으로 둡니다. 소비자는 이전 응답을 새 입력의 견적으로 표시하지 않습니다.
 */
export function useFinopsEstimate(body: ReturnType<typeof estimateRequest>): EstimateState {
  // 원본 위치를 몰라 요청을 만들지 못하면 부르지 않습니다 (key 가 null).
  const key = body === null ? null : JSON.stringify(body);
  // 마지막으로 받은 응답과 그 요청. 지금 입력(key)과 다르면 "조회 중" 입니다 (effect 안에서 상태를 바로 바꾸지 않음).
  const [last, setLast] = useState<{ key: string; data: FinopsEstimate | null; error: string | null } | null>(null);

  useEffect(() => {
    if (key === null) return;
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
