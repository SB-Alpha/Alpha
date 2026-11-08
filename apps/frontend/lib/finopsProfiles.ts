import type { TrafficInputs } from "@/lib/finopsApi";

// FinOps 카드에서 assessment 가 아직 읽지 못한 값(원본 앱 크기·RDS 등급·트래픽)을 채우는 예시 프로필.
// 실측이 아니라 가정값입니다. 화면은 이 값을 "예시값" 으로 표시하고, 사용자가 고친 값은 "사용자 입력" 으로 표시합니다.
// 앱 크기는 Fargate 가 허용하는 vCPU·메모리 조합, DB 등급은 RDS PostgreSQL 가격표에 있는 등급만 씁니다.

export interface FinopsUserInputs {
  sourceAppVcpu: number | null;
  sourceAppMemoryMib: number | null;
  dbInstanceClass: string | null;
  dbStorageGb: number | null;
  traffic: TrafficInputs;
}

export interface WorkloadProfile {
  id: "light" | "standard" | "heavy";
  label: string;
  description: string;
  inputs: FinopsUserInputs;
}

export const EMPTY_INPUTS: FinopsUserInputs = {
  sourceAppVcpu: null,
  sourceAppMemoryMib: null,
  dbInstanceClass: null,
  dbStorageGb: null,
  traffic: { rps: null, idlePercent: null, avgLatencyMs: null, readRatio: null, avgResponseKb: null },
};

export const WORKLOAD_PROFILES: readonly WorkloadProfile[] = [
  {
    id: "light",
    label: "소규모 내부 도구",
    description: "사내 관리자·백오피스. 업무 시간에만 가끔 요청",
    inputs: {
      sourceAppVcpu: 0.25,
      sourceAppMemoryMib: 512,
      dbInstanceClass: "db.t3.micro",
      dbStorageGb: 20,
      traffic: { rps: 2, idlePercent: 85, avgLatencyMs: 120, readRatio: 0.8, avgResponseKb: 8 },
    },
  },
  {
    id: "standard",
    label: "일반 웹 서비스",
    description: "B2C 웹·API. 낮에 꾸준한 요청, 밤에 한산",
    inputs: {
      sourceAppVcpu: 1,
      sourceAppMemoryMib: 2048,
      dbInstanceClass: "db.t3.medium",
      dbStorageGb: 50,
      traffic: { rps: 30, idlePercent: 40, avgLatencyMs: 150, readRatio: 0.7, avgResponseKb: 24 },
    },
  },
  {
    id: "heavy",
    label: "대규모 읽기 위주",
    description: "커머스·콘텐츠. 하루 종일 많은 조회 요청",
    inputs: {
      sourceAppVcpu: 2,
      sourceAppMemoryMib: 4096,
      dbInstanceClass: "db.m5.large",
      dbStorageGb: 200,
      traffic: { rps: 300, idlePercent: 10, avgLatencyMs: 80, readRatio: 0.9, avgResponseKb: 48 },
    },
  },
];

export const DEFAULT_PROFILE_ID: WorkloadProfile["id"] = "standard";

export const profileById = (id: WorkloadProfile["id"] | null): WorkloadProfile | null =>
  WORKLOAD_PROFILES.find((p) => p.id === id) ?? null;

/**
 * 프로필 값을 입력으로 씁니다. DB 디스크는 넣지 않습니다: 입력의 dbStorageGb 는 사용자가 고친 값만 담고,
 * 화면은 그릴 때마다 resolveDisk 로 "사용자 값 ?? 계획값 ?? 예시값" 을 고릅니다. 계획이 나중에 생겨도 예시값이 남지 않습니다.
 */
export function applyProfile(profile: WorkloadProfile): FinopsUserInputs {
  return { ...profile.inputs, dbStorageGb: null, traffic: { ...profile.inputs.traffic } };
}

/** plan 은 run 의 계획에서 읽은 실제 값입니다 (예: DB 디스크 계획값). */
export type ValueOrigin = "missing" | "plan" | "example" | "user";

/** DB 디스크: 사용자가 넣은 값, 없으면 계획값, 없으면 선택한 프로필의 예시값입니다. */
export function resolveDisk(
  userGb: number | null,
  planGb: number | null,
  profile: WorkloadProfile | null,
): { value: number | null; origin: ValueOrigin } {
  if (userGb !== null) return { value: userGb, origin: "user" };
  if (planGb !== null) return { value: planGb, origin: "plan" };
  if (profile) return { value: profile.inputs.dbStorageGb, origin: "example" };
  return { value: null, origin: "missing" };
}

/** 금액 계산에 예시값이 하나라도 들어갔는지 (DB 디스크는 resolveDisk 결과로 따로 봅니다). */
export function usesExampleValues(inputs: FinopsUserInputs, profile: WorkloadProfile | null): boolean {
  if (!profile) return false;
  const p = profile.inputs;
  const t = inputs.traffic;
  const pairs: [unknown, unknown][] = [
    [inputs.sourceAppVcpu, p.sourceAppVcpu],
    [inputs.sourceAppMemoryMib, p.sourceAppMemoryMib],
    [inputs.dbInstanceClass, p.dbInstanceClass],
    [t.rps, p.traffic.rps],
    [t.idlePercent, p.traffic.idlePercent],
    [t.avgLatencyMs, p.traffic.avgLatencyMs],
    [t.readRatio, p.traffic.readRatio],
    [t.avgResponseKb, p.traffic.avgResponseKb],
  ];
  return pairs.some(([value, example]) => valueOrigin(value, example) === "example");
}

/** 값이 비었으면 미제공, 선택한 프로필 값과 같으면 예시값, 그 밖에는 사용자 입력입니다. */
export function valueOrigin<T>(value: T | null, exampleValue: T | null | undefined): ValueOrigin {
  if (value === null) return "missing";
  return exampleValue !== undefined && exampleValue !== null && value === exampleValue ? "example" : "user";
}
