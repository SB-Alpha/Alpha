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
  projectIdHint: string;
  toConfig: (region: string, projectId?: string | null) => TargetInfraConfig;
}

const toConfigFor =
  (provider: TargetInfraType) =>
  (region: string, projectId?: string | null): TargetInfraConfig => {
    const trimmed = projectId?.trim();
    return trimmed ? { provider, region, project_id: trimmed } : { provider, region };
  };

const DEFAULT_PROJECT_ID_HINT = "비워둘 시 백엔드 기본 환경변수를 사용합니다.";

export const GcpTargetAdapter: TargetInfraAdapter = {
  provider: "gcp",
  label: "GCP Asia-Northeast3 (Cloud Run + Cloud SQL)",
  cloudName: "GCP",
  defaultRegion: "asia-northeast3",
  projectIdLabel: "목적지 프로젝트 ID (선택)",
  projectIdPlaceholder: "GCP 프로젝트 ID",
  projectIdHint: DEFAULT_PROJECT_ID_HINT,
  toConfig: toConfigFor("gcp"),
};

export const AzureTargetAdapter: TargetInfraAdapter = {
  provider: "azure",
  label: "Azure Korea Central (Container Apps + Flexible Server)",
  cloudName: "Azure",
  defaultRegion: "koreacentral",
  projectIdLabel: "목적지 구독 ID (선택)",
  projectIdPlaceholder: "00000000-0000-0000-0000-000000000000",
  projectIdHint: DEFAULT_PROJECT_ID_HINT,
  toConfig: toConfigFor("azure"),
};

export const AwsTargetAdapter: TargetInfraAdapter = {
  provider: "aws",
  label: "AWS Asia Pacific (Seoul) (ECS Fargate + RDS)",
  cloudName: "AWS",
  defaultRegion: "ap-northeast-2",
  projectIdLabel: "목적지 계정 ID (선택)",
  projectIdPlaceholder: "12자리 숫자 (예: 123456789012)",
  projectIdHint:
    "비워둘 시 백엔드 환경변수를 사용하며, 값과 환경변수가 모두 없으면 시작 전 오류('AWS 계정 ID가 없습니다')로 중단됩니다.",
  toConfig: toConfigFor("aws"),
};

export const NcpTargetAdapter: TargetInfraAdapter = {
  provider: "ncp",
  label: "NCP (Naver Cloud Platform)",
  cloudName: "NCP",
  defaultRegion: "KR",
  projectIdLabel: "목적지 프로젝트 ID (선택)",
  projectIdPlaceholder: "ncp-project",
  projectIdHint: DEFAULT_PROJECT_ID_HINT,
  toConfig: toConfigFor("ncp"),
};

export const TARGET_ADAPTERS: TargetInfraAdapter[] = [
  GcpTargetAdapter,
  AzureTargetAdapter,
  AwsTargetAdapter,
  NcpTargetAdapter,
];

// 고를 수 있는 목적지는 백엔드에 어댑터가 등록된 클라우드뿐입니다 (GET /runs/targets).
// 손으로 옮겨 적지 않고 백엔드 목록을 그대로 씁니다. 목록을 못 읽으면 아무것도 고를 수 없습니다.
export interface TargetAvailability {
  provider: TargetInfraType;
  available: boolean;
}

export async function fetchTargetAvailability(): Promise<Set<TargetInfraType>> {
  const res = await fetch("/api/runs/targets", { cache: "no-store" });
  if (!res.ok) throw new Error(`목적지 목록을 읽지 못했습니다 (HTTP ${res.status})`);
  const rows = (await res.json()) as TargetAvailability[];
  return new Set(rows.filter((r) => r.available).map((r) => r.provider));
}

export function adapterFor(provider: TargetInfraType | null | undefined): TargetInfraAdapter {
  return TARGET_ADAPTERS.find((adapter) => adapter.provider === provider) ?? GcpTargetAdapter;
}
