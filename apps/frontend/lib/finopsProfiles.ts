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

/** 프로필 값을 입력으로 씁니다. 계획에 DB 디스크 값이 있으면 그 실제 값을 두고 예시 디스크는 넣지 않습니다. */
export function applyProfile(profile: WorkloadProfile, planDiskGb: number | null): FinopsUserInputs {
  return {
    ...profile.inputs,
    dbStorageGb: planDiskGb !== null ? null : profile.inputs.dbStorageGb,
    traffic: { ...profile.inputs.traffic },
  };
}

/** plan 은 run 의 계획에서 읽은 실제 값입니다 (예: DB 디스크 계획값). */
export type ValueOrigin = "missing" | "plan" | "example" | "user";

/** 값이 비었으면 미제공, 선택한 프로필 값과 같으면 예시값, 그 밖에는 사용자 입력입니다. */
export function valueOrigin<T>(value: T | null, exampleValue: T | null | undefined): ValueOrigin {
  if (value === null) return "missing";
  return exampleValue !== undefined && exampleValue !== null && value === exampleValue ? "example" : "user";
}
