// 목적지 클라우드 어댑터. 입력 폼과 화면이 쓰는 표시 정보와 요청 값을 한곳에 둡니다.
// 계약은 docs/TARGET_ADAPTER_SPEC.md 에 있습니다.

export type TargetInfraType = "gcp" | "azure" | "aws" | "ncp";

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
  projectIdLabel: string;
  projectIdPlaceholder: string;
  // backend `target_adapters.TARGET_ADAPTERS` 에 등록된 클라우드만 true 입니다. 등록 전에는 API 도 거절합니다.
  isAvailable: boolean;
  toConfig: (region: string, projectId?: string | null) => TargetInfraConfig;
}

const toConfigFor =
  (provider: TargetInfraType) =>
  (region: string, projectId?: string | null): TargetInfraConfig => {
    const trimmed = projectId?.trim();
    return trimmed ? { provider, region, project_id: trimmed } : { provider, region };
  };

export const GcpTargetAdapter: TargetInfraAdapter = {
  provider: "gcp",
  label: "GCP Asia-Northeast3 (Cloud Run + Cloud SQL)",
  cloudName: "GCP",
  defaultRegion: "asia-northeast3",
  projectIdLabel: "프로젝트 ID",
  projectIdPlaceholder: "my-gcp-project",
  isAvailable: true,
  toConfig: toConfigFor("gcp"),
};

export const AzureTargetAdapter: TargetInfraAdapter = {
  provider: "azure",
  label: "Azure Korea Central (Container Apps + Flexible Server)",
  cloudName: "Azure",
  defaultRegion: "koreacentral",
  projectIdLabel: "구독 ID",
  projectIdPlaceholder: "00000000-0000-0000-0000-000000000000",
  isAvailable: true,
  toConfig: toConfigFor("azure"),
};

export const AwsTargetAdapter: TargetInfraAdapter = {
  provider: "aws",
  label: "AWS Asia Pacific (Seoul) (ECS Fargate + RDS)",
  cloudName: "AWS",
  defaultRegion: "ap-northeast-2",
  projectIdLabel: "계정 ID(12자리)",
  projectIdPlaceholder: "123456789012",
  isAvailable: false,
  toConfig: toConfigFor("aws"),
};

export const NcpTargetAdapter: TargetInfraAdapter = {
  provider: "ncp",
  label: "NCP (Naver Cloud Platform)",
  cloudName: "NCP",
  defaultRegion: "KR",
  projectIdLabel: "프로젝트 ID",
  projectIdPlaceholder: "ncp-project",
  isAvailable: false,
  toConfig: toConfigFor("ncp"),
};

export const TARGET_ADAPTERS: TargetInfraAdapter[] = [
  GcpTargetAdapter,
  AzureTargetAdapter,
  AwsTargetAdapter,
  NcpTargetAdapter,
];

export function adapterFor(provider: TargetInfraType | null | undefined): TargetInfraAdapter {
  return TARGET_ADAPTERS.find((adapter) => adapter.provider === provider) ?? GcpTargetAdapter;
}
