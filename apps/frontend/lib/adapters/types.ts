// 목적지 클라우드 어댑터. 입력 폼과 화면이 쓰는 표시 정보와 요청 값을 한곳에 둡니다.
// 계약은 docs/TARGET_ADAPTER_SPEC.md 에 있습니다.

export type TargetInfraType = "gcp" | "azure" | "ncp";

// backend `workflows/schemas.py` 의 TargetInfraConfig 와 같은 모양입니다.
export interface TargetInfraConfig {
  provider: TargetInfraType;
  region?: string | null;
  project_id?: string | null;
  extra_params?: Record<string, unknown>;
}

export interface TargetInfraAdapter {
  provider: TargetInfraType;
  label: string;
  cloudName: string;
  defaultRegion: string;
  // backend `target_adapters.TARGET_ADAPTERS` 에 등록된 클라우드만 true 입니다. 등록 전에는 API 도 거절합니다.
  isAvailable: boolean;
  toConfig: (region: string) => TargetInfraConfig;
}

const toConfigFor = (provider: TargetInfraType) => (region: string) => ({ provider, region });

export const GcpTargetAdapter: TargetInfraAdapter = {
  provider: "gcp",
  label: "GCP Asia-Northeast3 (Cloud Run + Cloud SQL)",
  cloudName: "GCP",
  defaultRegion: "asia-northeast3",
  isAvailable: true,
  toConfig: toConfigFor("gcp"),
};

export const AzureTargetAdapter: TargetInfraAdapter = {
  provider: "azure",
  label: "Azure Korea Central (Container Apps + Flexible Server)",
  cloudName: "Azure",
  defaultRegion: "koreacentral",
  isAvailable: false,
  toConfig: toConfigFor("azure"),
};

export const NcpTargetAdapter: TargetInfraAdapter = {
  provider: "ncp",
  label: "NCP (Naver Cloud Platform)",
  cloudName: "NCP",
  defaultRegion: "KR",
  isAvailable: false,
  toConfig: toConfigFor("ncp"),
};

export const TARGET_ADAPTERS: TargetInfraAdapter[] = [GcpTargetAdapter, AzureTargetAdapter, NcpTargetAdapter];

export function adapterFor(provider: TargetInfraType | null | undefined): TargetInfraAdapter {
  return TARGET_ADAPTERS.find((adapter) => adapter.provider === provider) ?? GcpTargetAdapter;
}
