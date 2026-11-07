// 클라우드별 월 비용 추정. 서울 리전 공시가와 아래 가정으로 계산하며, 실제 청구액이나 측정값이 아닙니다.
// 공시가의 출처와 확인일은 PRICE_SOURCES, PRICE_AS_OF 에 있습니다. 근거가 없는 값은 null("미제공")로 둡니다.

export type TargetProvider = "gcp" | "azure";
export type DbClass = "db.t3.micro" | "db.t3.small" | "db.t3.medium";
export type CostVerdict = "COST_LOWER" | "SIMILAR" | "COST_HIGHER";

export interface CostBreakdown {
  compute: number;
  database: number;
  egress: number;
  total: number;
}

export interface CarbonFacts {
  gridIntensity: number | null; // gCO2eq/kWh. 공개값이 없으면 null
  cfePercent: number | null; // 공개값이 없으면 null
}

export interface ProviderFinOpsProfile {
  provider: "aws" | TargetProvider;
  providerName: string;
  region: string;
  regionName: string;
  services: string;
  computeNote: string;
  cost: CostBreakdown;
  carbon: CarbonFacts;
}

export interface FinOpsComparisonResult {
  source: ProviderFinOpsProfile;
  target: ProviderFinOpsProfile;
  monthlyDeltaUsd: number; // 목적지 - 원본. 양수면 비용이 늘어납니다
  deltaPercent: number;
  annualDeltaUsd: number;
  verdict: CostVerdict;
  verdictReason: string;
}

export interface FinOpsInputParams {
  rps: number; // 활성 시간 동안의 초당 평균 요청 수
  idlePercent: number; // 요청이 없는 시간 비율 (0 ~ 80%)
  avgRequestMs: number; // 요청당 처리 시간
  dbSizeGb: number;
  dbClass: DbClass;
  cpuCount: number;
  memoryMb: number;
  targetProvider: TargetProvider;
}

export interface PriceSource {
  label: string;
  url: string;
}

export const PRICE_AS_OF = "2026-11-07";
export const CARBON_DATA_YEAR = 2024;
export const SIMILAR_COST_BAND_PERCENT = 15;
const HOURS_PER_MONTH = 730;
const SECONDS_PER_HOUR = 3600;
const RESPONSE_KB_PER_REQUEST = 12; // 가정: 실측 트래픽이 없어 요청당 응답 크기를 고정합니다

export const FINOPS_DEFAULTS = {
  rps: 10,
  idlePercent: 40,
  avgRequestMs: 100,
  dbSizeGb: 50,
  dbClass: "db.t3.medium",
  cpuCount: 1,
  memoryMb: 2048,
} as const satisfies Omit<FinOpsInputParams, "targetProvider">;

// ECS Fargate(Linux/x86), RDS Single-AZ, 인터넷 전송 첫 10TB 구간
const AWS_SEOUL = {
  fargateVcpuHour: 0.04656,
  fargateGbHour: 0.00511,
  rdsStorageGbMonth: 0.131,
  egressGb: 0.126,
};

// Cloud Run 요청 기반 과금(Tier 2 활성 시간), Cloud SQL Enterprise, Premium Tier 한국 목적지 첫 10TiB 구간.
// 팀 Terraform 이 min_instance_count = 0 이고 CPU 상시 할당을 켜지 않아 요청 기반 과금입니다.
const GCP_SEOUL = {
  cloudRunVcpuSecond: 0.0000336,
  cloudRunGibSecond: 0.0000035,
  requestsPerMillion: 0.4,
  sqlSsdGibMonth: 0.221,
  egressGb: 0.19,
};

// Container Apps 소비 플랜, PostgreSQL Flexible Server, 인터넷 전송(Microsoft 네트워크) 첫 구간.
// 최소 복제본 2개는 infra/azure 의 app_min_replicas 기본값입니다. 0 으로 두면 이 추정보다 낮아집니다.
const AZURE_KOREA_CENTRAL = {
  appsActiveVcpuSecond: 0.000024,
  appsIdleVcpuSecond: 0.000003,
  appsGibSecond: 0.000003,
  requestsPerMillion: 0.4,
  minReplicas: 2,
  pgStorageGbMonth: 0.131,
  egressGb: 0.12,
};

// 목적지 등급은 원본 메모리 이상인 가장 작은 등급입니다 (assessment/rules.py 와 같은 기준).
// Cloud SQL 맞춤 등급은 vCPU $0.0537/시간 + 메모리 $0.0091/GiB·시간 으로 계산했습니다.
export const DB_CLASSES: Record<
  DbClass,
  { awsHour: number; gcpTier: string; gcpHour: number; azureSku: string; azureHour: number }
> = {
  "db.t3.micro": { awsHour: 0.028, gcpTier: "db-g1-small", gcpHour: 0.0455, azureSku: "B1ms", azureHour: 0.026 },
  "db.t3.small": { awsHour: 0.056, gcpTier: "db-custom-1-3840", gcpHour: 0.087825, azureSku: "B1ms", azureHour: 0.026 },
  "db.t3.medium": { awsHour: 0.112, gcpTier: "db-custom-1-4096", gcpHour: 0.0901, azureSku: "B2s", azureHour: 0.104 },
};

// Google 이 공개한 서울 리전 값입니다. AWS·Azure 는 같은 형식의 리전별 공개값을 확인하지 못했습니다.
const GCP_SEOUL_CARBON: CarbonFacts = { gridIntensity: 356.57, cfePercent: 37 };
const CARBON_NOT_PROVIDED: CarbonFacts = { gridIntensity: null, cfePercent: null };

export const PRICE_SOURCES: readonly PriceSource[] = [
  { label: "AWS Fargate (Price List, 서울)", url: "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonECS/current/ap-northeast-2/index.json" },
  { label: "AWS RDS (Price List, 서울)", url: "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/ap-northeast-2/index.json" },
  { label: "AWS 데이터 전송 (Price List, 서울)", url: "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSDataTransfer/current/ap-northeast-2/index.json" },
  { label: "Cloud Run 가격", url: "https://cloud.google.com/run/pricing" },
  { label: "Cloud SQL 가격", url: "https://cloud.google.com/sql/pricing" },
  { label: "Google Cloud 네트워크 가격", url: "https://cloud.google.com/vpc/network-pricing" },
  { label: "Azure Retail Prices API", url: "https://prices.azure.com/api/retail/prices" },
  { label: "Google 리전별 탄소 데이터", url: "https://cloud.google.com/sustainability/region-carbon" },
];

export const EXCLUDED_COSTS: readonly string[] = [
  "로드밸런서, NAT, 로그, 백업",
  "Cloudflare Worker",
  "CDC 노드와 VPN",
  "이전 기간의 양쪽 동시 가동",
  "무료 사용량과 약정 할인",
];

const roundUsd = (value: number): number => Number(value.toFixed(2));

function breakdown(compute: number, database: number, egress: number): CostBreakdown {
  return {
    compute: roundUsd(compute),
    database: roundUsd(database),
    egress: roundUsd(egress),
    total: roundUsd(compute + database + egress),
  };
}

function judge(deltaUsd: number, deltaPercent: number): { verdict: CostVerdict; reason: string } {
  const amount = Math.abs(deltaUsd).toFixed(2);
  if (deltaPercent <= -SIMILAR_COST_BAND_PERCENT) {
    return { verdict: "COST_LOWER", reason: `공시가 기준 월 $${amount} 적게 듭니다.` };
  }
  if (deltaPercent >= SIMILAR_COST_BAND_PERCENT) {
    return { verdict: "COST_HIGHER", reason: `공시가 기준 월 $${amount} 더 듭니다.` };
  }
  return { verdict: "SIMILAR", reason: `차이가 ±${SIMILAR_COST_BAND_PERCENT}% 안이라 입력값에 따라 달라집니다.` };
}

/** 같은 부하를 AWS 와 목적지에 올렸을 때의 월 비용을 공시가로 추정합니다. */
export function computeFinOpsComparison(params: FinOpsInputParams): FinOpsComparisonResult {
  const { rps, idlePercent, avgRequestMs, dbSizeGb, dbClass, cpuCount, memoryMb, targetProvider } = params;
  const memoryGib = memoryMb / 1024;
  const db = DB_CLASSES[dbClass];

  const idleRatio = Math.min(0.8, Math.max(0, idlePercent / 100));
  const activeHours = HOURS_PER_MONTH * (1 - idleRatio);
  const monthlyRequests = rps * SECONDS_PER_HOUR * activeHours;
  const egressGb = (monthlyRequests * RESPONSE_KB_PER_REQUEST) / (1024 * 1024);
  // 요청이 무작위로 들어올 때 하나 이상 처리 중인 시간 비율입니다.
  const busyRatio = 1 - Math.exp(-rps * (avgRequestMs / 1000));
  const busySeconds = activeHours * busyRatio * SECONDS_PER_HOUR;

  const source: ProviderFinOpsProfile = {
    provider: "aws",
    providerName: "AWS",
    region: "ap-northeast-2",
    regionName: "Seoul (ap-northeast-2)",
    services: `ECS Fargate + RDS (${dbClass})`,
    computeNote: "상시 가동",
    cost: breakdown(
      (cpuCount * AWS_SEOUL.fargateVcpuHour + memoryGib * AWS_SEOUL.fargateGbHour) * HOURS_PER_MONTH,
      db.awsHour * HOURS_PER_MONTH + dbSizeGb * AWS_SEOUL.rdsStorageGbMonth,
      egressGb * AWS_SEOUL.egressGb,
    ),
    carbon: CARBON_NOT_PROVIDED,
  };

  const target: ProviderFinOpsProfile =
    targetProvider === "gcp"
      ? {
          provider: "gcp",
          providerName: "GCP",
          region: "asia-northeast3",
          regionName: "Seoul (asia-northeast3)",
          services: `Cloud Run + Cloud SQL (${db.gcpTier})`,
          computeNote: "요청 처리 시간 + 요청 수수료",
          cost: breakdown(
            (cpuCount * GCP_SEOUL.cloudRunVcpuSecond + memoryGib * GCP_SEOUL.cloudRunGibSecond) * busySeconds +
              (monthlyRequests / 1_000_000) * GCP_SEOUL.requestsPerMillion,
            db.gcpHour * HOURS_PER_MONTH + dbSizeGb * GCP_SEOUL.sqlSsdGibMonth,
            egressGb * GCP_SEOUL.egressGb,
          ),
          carbon: GCP_SEOUL_CARBON,
        }
      : {
          provider: "azure",
          providerName: "Azure",
          region: "koreacentral",
          regionName: "Korea Central (koreacentral)",
          services: `Container Apps + PostgreSQL (${db.azureSku})`,
          computeNote: `최소 복제본 ${AZURE_KOREA_CENTRAL.minReplicas}개 유지 + 요청 수수료`,
          cost: breakdown(
            AZURE_KOREA_CENTRAL.minReplicas *
              (cpuCount * AZURE_KOREA_CENTRAL.appsIdleVcpuSecond + memoryGib * AZURE_KOREA_CENTRAL.appsGibSecond) *
              HOURS_PER_MONTH *
              SECONDS_PER_HOUR +
              cpuCount *
                (AZURE_KOREA_CENTRAL.appsActiveVcpuSecond - AZURE_KOREA_CENTRAL.appsIdleVcpuSecond) *
                busySeconds +
              (monthlyRequests / 1_000_000) * AZURE_KOREA_CENTRAL.requestsPerMillion,
            db.azureHour * HOURS_PER_MONTH + dbSizeGb * AZURE_KOREA_CENTRAL.pgStorageGbMonth,
            egressGb * AZURE_KOREA_CENTRAL.egressGb,
          ),
          carbon: CARBON_NOT_PROVIDED,
        };

  const deltaUsd = target.cost.total - source.cost.total;
  const deltaPercent = (deltaUsd / source.cost.total) * 100;
  const { verdict, reason } = judge(deltaUsd, deltaPercent);

  return {
    source,
    target,
    monthlyDeltaUsd: roundUsd(deltaUsd),
    deltaPercent: Number(deltaPercent.toFixed(1)),
    annualDeltaUsd: roundUsd(deltaUsd * 12),
    verdict,
    verdictReason: reason,
  };
}
