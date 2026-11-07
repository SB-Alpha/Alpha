// 클라우드별 월 비용 추정. 서울 리전 공시가와 아래 가정으로 계산하며, 실제 청구액이나 측정값이 아닙니다.
// 공시가의 출처와 확인일은 PRICE_SOURCES, PRICE_AS_OF 에 있습니다. 근거가 없는 값은 null("미제공")로 둡니다.

export type TargetProvider = "gcp" | "azure" | "aws";
export type DbClass = "db.t3.micro" | "db.t3.small" | "db.t3.medium";
export type DbBasis = "same_memory" | "as_deployed";
export type CostVerdict = "COST_LOWER" | "COST_LOWER_CONDITIONAL" | "SIMILAR" | "COST_HIGHER";
export type TierOrigin = "계획값" | "Terraform 기본값" | "Azure 기본값" | "AWS 기본값" | "같은 메모리 기준";
export type InputOrigin = "계획값" | "가정" | "사용자 입력";

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

export interface DbFacts {
  name: string;
  memoryLabel: string;
  memoryGb: number;
  cpuLabel: string;
}

export interface TargetDbFacts extends DbFacts {
  tierOrigin: TierOrigin;
}

export interface FinOpsComparisonResult {
  source: ProviderFinOpsProfile;
  target: ProviderFinOpsProfile;
  monthlyDeltaUsd: number; // 목적지 - 원본. 양수면 비용이 늘어납니다
  deltaPercent: number;
  annualDeltaUsd: number;
  verdict: CostVerdict;
  verdictReason: string;
  basis: DbBasis;
  sourceDb: DbFacts;
  targetDb: TargetDbFacts;
  condition: string;
}

export interface BothBases {
  sameMemory: FinOpsComparisonResult;
  asDeployed: FinOpsComparisonResult | null; // 계획의 등급이 가격표에 없으면 null
}

export interface FinOpsInputParams {
  rps: number; // 활성 시간 동안의 초당 평균 요청 수
  idlePercent: number; // 요청이 없는 시간 비율 (0 ~ 80%)
  avgRequestMs: number; // 요청당 처리 시간
  dbSizeGb: number;
  dbClass: DbClass;
  cpuCount: number; // 목적지 앱 크기
  memoryMb: number; // 목적지 앱 크기
  sourceCpuCount: number; // 원본 AWS 앱 크기
  sourceMemoryMb: number; // 원본 AWS 앱 크기
  targetProvider: TargetProvider;
  dbBasis: DbBasis;
  planDbTier: string | null; // 계획의 target_infra.db_tier
}

export interface PriceSource {
  label: string;
  url: string;
}

export const NOT_PROVIDED = "미제공";
export const RIGHTSIZING_NOT_PROVIDED = "크기 줄이기 제안: 미제공 (사용량 근거 없음)";
export const PRICE_AS_OF = "2026-11-07";
export const CARBON_DATA_YEAR = 2025;
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
  sourceCpuCount: 1,
  sourceMemoryMb: 2048,
  dbBasis: "same_memory",
  planDbTier: null,
} as const satisfies Omit<FinOpsInputParams, "targetProvider">;

export const SLIDER_RANGES = {
  rps: { min: 1, max: 100, step: 1 },
  idlePercent: { min: 0, max: 80, step: 5 },
  avgRequestMs: { min: 20, max: 1000, step: 20 },
} as const;

export type FlipAxis = keyof typeof SLIDER_RANGES;

const FLIP_AXIS_LABEL: Record<FlipAxis, string> = {
  rps: "RPS",
  idlePercent: "유휴 비율(%)",
  avgRequestMs: "요청당 처리 시간(ms)",
};

export const VERDICT_LABEL: Record<CostVerdict, string> = {
  COST_LOWER: "비용 낮음 (가정 기준)",
  COST_LOWER_CONDITIONAL: "DB 축소 조건부",
  SIMILAR: "비용 차이 작음",
  COST_HIGHER: "비용 불리",
};

const PRICED_REGION: Record<TargetProvider, string> = {
  gcp: "asia-northeast3",
  azure: "koreacentral",
  aws: "ap-northeast-2",
};

// ECS Fargate(Linux/x86), RDS Single-AZ, 인터넷 전송 첫 10TB 구간
const AWS_SEOUL = {
  fargateVcpuHour: 0.04656,
  fargateGbHour: 0.00511,
  rdsStorageGbMonth: 0.131,
  egressGb: 0.126,
};

// Cloud Run 요청 기반 과금(Tier 2 활성 시간), Cloud SQL Enterprise, Premium Tier 한국 목적지 첫 10TiB 구간.
// 요청 기반 과금으로 가정합니다. Terraform 이 cpu_idle 을 지정하지 않아 실제 배포 모드는 확인이 필요합니다.
const GCP_SEOUL = {
  cloudRunVcpuSecond: 0.0000336,
  cloudRunGibSecond: 0.0000035,
  requestsPerMillion: 0.4,
  sqlSsdGibMonth: 0.221,
  egressGb: 0.19,
};

// Container Apps 소비 플랜, PostgreSQL Flexible Server, 인터넷 전송(Microsoft 네트워크) 첫 구간.
// 최소 복제본 2개는 upstream #52 의 app_min_replicas 기본값입니다. 0 으로 두면 이 추정보다 낮아집니다.
const AZURE_KOREA_CENTRAL = {
  appsActiveVcpuSecond: 0.000024,
  appsIdleVcpuSecond: 0.000003,
  appsGibSecond: 0.000003,
  requestsPerMillion: 0.4,
  minReplicas: 2,
  pgStorageGbMonth: 0.131,
  egressGb: 0.12,
};

// infra/terraform/gcp/variables.tf 의 db_tier 기본값입니다.
const TERRAFORM_DEFAULT_DB_TIER = "db-g1-small";
// infra/azure 의 기본값입니다.
const AZURE_DEFAULT_DB_SKU = "B1ms";
// AWS RDS 비교용 기본 등급입니다.
const AWS_DEFAULT_DB_CLASS: DbClass = "db.t3.small";

interface DbTier {
  provider: TargetProvider;
  hour: number;
  memoryLabel: string;
  memoryGb: number;
  cpuLabel: string;
}

// Cloud SQL 맞춤 등급은 vCPU $0.0537/시간 + 메모리 $0.0091/GiB·시간 으로 계산했습니다.
export const DB_TIERS: Record<string, DbTier> = {
  "db-g1-small": { provider: "gcp", hour: 0.0455, memoryLabel: "1.7GB", memoryGb: 1.7, cpuLabel: "공유 코어" },
  "db-custom-1-3840": { provider: "gcp", hour: 0.087825, memoryLabel: "3.75GiB", memoryGb: 3.75, cpuLabel: "1 vCPU" },
  "db-custom-1-4096": { provider: "gcp", hour: 0.0901, memoryLabel: "4GiB", memoryGb: 4, cpuLabel: "1 vCPU" },
  B1ms: { provider: "azure", hour: 0.026, memoryLabel: "2GiB", memoryGb: 2, cpuLabel: "1 vCore 버스터블" },
  B2s: { provider: "azure", hour: 0.104, memoryLabel: "4GiB", memoryGb: 4, cpuLabel: "2 vCore 버스터블" },
  "db.t3.micro": { provider: "aws", hour: 0.028, memoryLabel: "1GiB", memoryGb: 1, cpuLabel: "2 vCPU 버스터블" },
  "db.t3.small": { provider: "aws", hour: 0.056, memoryLabel: "2GiB", memoryGb: 2, cpuLabel: "2 vCPU 버스터블" },
  "db.t3.medium": { provider: "aws", hour: 0.112, memoryLabel: "4GiB", memoryGb: 4, cpuLabel: "2 vCPU 버스터블" },
};

// 같은 메모리 등급 비교입니다. 실제 배포 등급과 다를 수 있어 as_deployed 로 따로 계산합니다.
// 원본 t3 등급의 vCPU 와 메모리는 AWS Price List 값입니다.
export const DB_CLASSES: Record<
  DbClass,
  { awsHour: number; gcpTier: string; azureSku: string; memoryLabel: string; memoryGb: number; cpuLabel: string }
> = {
  "db.t3.micro": { awsHour: 0.028, gcpTier: "db-g1-small", azureSku: "B1ms", memoryLabel: "1GiB", memoryGb: 1, cpuLabel: "2 vCPU 버스터블" },
  "db.t3.small": { awsHour: 0.056, gcpTier: "db-custom-1-3840", azureSku: "B1ms", memoryLabel: "2GiB", memoryGb: 2, cpuLabel: "2 vCPU 버스터블" },
  "db.t3.medium": { awsHour: 0.112, gcpTier: "db-custom-1-4096", azureSku: "B2s", memoryLabel: "4GiB", memoryGb: 4, cpuLabel: "2 vCPU 버스터블" },
};

// Google 이 공개한 서울 리전 값입니다 (region-carbon-info yearly/2025.csv). AWS·Azure 는 같은 형식의 리전별 공개값을 확인하지 못했습니다.
const GCP_SEOUL_CARBON: CarbonFacts = { gridIntensity: 359.05, cfePercent: 37 };
const CARBON_NOT_PROVIDED: CarbonFacts = { gridIntensity: null, cfePercent: null };

export const PRICE_SOURCES: readonly PriceSource[] = [
  { label: "AWS Fargate (Price List, 서울)", url: "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonECS/current/ap-northeast-2/index.json" },
  { label: "AWS RDS (Price List, 서울)", url: "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/ap-northeast-2/index.json" },
  { label: "AWS 데이터 전송 (Price List, 서울)", url: "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSDataTransfer/current/ap-northeast-2/index.json" },
  { label: "Cloud Run 가격", url: "https://cloud.google.com/run/pricing" },
  { label: "Cloud SQL 가격", url: "https://cloud.google.com/sql/pricing" },
  { label: "Google Cloud SQL 인스턴스 설정 문서", url: "https://docs.cloud.google.com/sql/docs/postgres/instance-settings" },
  { label: "Google Cloud 네트워크 가격", url: "https://cloud.google.com/vpc/network-pricing" },
  { label: "Azure Retail Prices API", url: "https://prices.azure.com/api/retail/prices" },
  { label: "Microsoft Learn PostgreSQL 컴퓨팅 문서", url: "https://learn.microsoft.com/en-us/azure/postgresql/compute-storage/concepts-compute" },
  { label: `Google 리전별 탄소 데이터 (${CARBON_DATA_YEAR} CSV)`, url: "https://raw.githubusercontent.com/GoogleCloudPlatform/region-carbon-info/main/data/yearly/2025.csv" },
  { label: "Google 리전별 탄소 안내", url: "https://cloud.google.com/sustainability/region-carbon" },
];

export const EXCLUDED_COSTS: readonly string[] = [
  "로드밸런서, NAT, 로그, 백업",
  "Cloudflare Worker",
  "CDC 노드와 VPN",
  "이전 기간의 양쪽 동시 가동",
  "무료 사용량과 약정 할인",
  "Azure 환경 단위 요금 (Environment Management 등)",
];

const roundUsd = (value: number): number => Number(value.toFixed(2));

export const formatUsd = (value: number): string =>
  `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
export const signedUsd = (value: number): string => `${value >= 0 ? "+" : "-"}${formatUsd(Math.abs(value))}`;
// 반올림으로 -0 이 되어도 금액과 부호가 어긋나지 않게 합니다.
export const signedPercent = (value: number): string =>
  `${value < 0 || Object.is(value, -0) ? "-" : "+"}${Math.abs(value).toFixed(1)}%`;

function breakdown(compute: number, database: number, egress: number): CostBreakdown {
  return {
    compute: roundUsd(compute),
    database: roundUsd(database),
    egress: roundUsd(egress),
    total: roundUsd(compute + database + egress),
  };
}

function judge(deltaUsd: number, deltaPercent: number, isSmallerDb: boolean): { verdict: CostVerdict; reason: string } {
  const amount = Math.abs(deltaUsd).toFixed(2);
  if (deltaPercent <= -SIMILAR_COST_BAND_PERCENT) {
    return isSmallerDb
      ? {
          verdict: "COST_LOWER_CONDITIONAL",
          reason: `DB 를 줄였을 때 공시가 기준 월 $${amount} 적게 듭니다. 사용량 근거 미제공입니다.`,
        }
      : {
          verdict: "COST_LOWER",
          reason: `공시가 기준 월 $${amount} 적게 듭니다. 가정 기준이며 사용량 근거 미제공입니다 (RPS, 유휴 비율, 스케일-투-제로, 원본 크기).`,
        };
  }
  if (deltaPercent >= SIMILAR_COST_BAND_PERCENT) {
    return { verdict: "COST_HIGHER", reason: `공시가 기준 월 $${amount} 더 듭니다.` };
  }
  return { verdict: "SIMILAR", reason: `차이가 ±${SIMILAR_COST_BAND_PERCENT}% 안이라 입력값에 따라 달라집니다.` };
}

interface ResolvedDb extends TargetDbFacts {
  hour: number;
}

function resolveTargetDb({ targetProvider, dbClass, dbBasis, planDbTier }: FinOpsInputParams): ResolvedDb | null {
  const sameMemoryName =
    targetProvider === "gcp"
      ? DB_CLASSES[dbClass].gcpTier
      : targetProvider === "azure"
        ? DB_CLASSES[dbClass].azureSku
        : dbClass;
  const deployedName =
    targetProvider === "gcp"
      ? (planDbTier ?? TERRAFORM_DEFAULT_DB_TIER)
      : targetProvider === "azure"
        ? AZURE_DEFAULT_DB_SKU
        : AWS_DEFAULT_DB_CLASS;
  const deployedOrigin: TierOrigin =
    targetProvider === "azure"
      ? "Azure 기본값"
      : targetProvider === "aws"
        ? "AWS 기본값"
        : planDbTier !== null
          ? "계획값"
          : "Terraform 기본값";
  const name = dbBasis === "same_memory" ? sameMemoryName : deployedName;
  const tierOrigin: TierOrigin = dbBasis === "same_memory" ? "같은 메모리 기준" : deployedOrigin;

  // 표에 없거나 다른 클라우드 등급이면 값을 만들지 않습니다.
  if (!Object.hasOwn(DB_TIERS, name) || DB_TIERS[name].provider !== targetProvider) return null;
  const { hour, memoryLabel, memoryGb, cpuLabel } = DB_TIERS[name];
  return { name, hour, memoryLabel, memoryGb, cpuLabel, tierOrigin };
}

interface Load {
  monthlyRequests: number;
  egressGb: number;
  busySeconds: number;
}

function loadOf({ rps, idlePercent, avgRequestMs }: FinOpsInputParams): Load {
  const idleRatio = Math.min(0.8, Math.max(0, idlePercent / 100));
  const activeHours = HOURS_PER_MONTH * (1 - idleRatio);
  const monthlyRequests = rps * SECONDS_PER_HOUR * activeHours;
  // 요청이 무작위로 들어올 때 하나 이상 처리 중인 시간 비율입니다.
  const busyRatio = 1 - Math.exp(-rps * (avgRequestMs / 1000));
  return {
    monthlyRequests,
    egressGb: (monthlyRequests * RESPONSE_KB_PER_REQUEST) / (1024 * 1024),
    busySeconds: activeHours * busyRatio * SECONDS_PER_HOUR,
  };
}

function awsProfile(params: FinOpsInputParams, load: Load): ProviderFinOpsProfile {
  const { sourceCpuCount, sourceMemoryMb, dbClass, dbSizeGb } = params;
  return {
    provider: "aws",
    providerName: "AWS",
    region: "ap-northeast-2",
    regionName: "Seoul (ap-northeast-2)",
    services: `ECS Fargate + RDS (${dbClass})`,
    computeNote: "상시 가동",
    cost: breakdown(
      (sourceCpuCount * AWS_SEOUL.fargateVcpuHour + (sourceMemoryMb / 1024) * AWS_SEOUL.fargateGbHour) * HOURS_PER_MONTH,
      DB_CLASSES[dbClass].awsHour * HOURS_PER_MONTH + dbSizeGb * AWS_SEOUL.rdsStorageGbMonth,
      load.egressGb * AWS_SEOUL.egressGb,
    ),
    carbon: CARBON_NOT_PROVIDED,
  };
}

function gcpProfile(params: FinOpsInputParams, load: Load, db: ResolvedDb): ProviderFinOpsProfile {
  const { cpuCount, memoryMb, dbSizeGb } = params;
  return {
    provider: "gcp",
    providerName: "GCP",
    region: PRICED_REGION.gcp,
    regionName: `Seoul (${PRICED_REGION.gcp})`,
    services: `Cloud Run + Cloud SQL (${db.name})`,
    computeNote: "요청 처리 시간 + 요청 수수료",
    cost: breakdown(
      (cpuCount * GCP_SEOUL.cloudRunVcpuSecond + (memoryMb / 1024) * GCP_SEOUL.cloudRunGibSecond) * load.busySeconds +
        (load.monthlyRequests / 1_000_000) * GCP_SEOUL.requestsPerMillion,
      db.hour * HOURS_PER_MONTH + dbSizeGb * GCP_SEOUL.sqlSsdGibMonth,
      load.egressGb * GCP_SEOUL.egressGb,
    ),
    carbon: GCP_SEOUL_CARBON,
  };
}

function azureProfile(params: FinOpsInputParams, load: Load, db: ResolvedDb): ProviderFinOpsProfile {
  const { cpuCount, memoryMb, dbSizeGb } = params;
  const prices = AZURE_KOREA_CENTRAL;
  const idleCost =
    prices.minReplicas *
    (cpuCount * prices.appsIdleVcpuSecond + (memoryMb / 1024) * prices.appsGibSecond) *
    HOURS_PER_MONTH *
    SECONDS_PER_HOUR;
  const activeCost = cpuCount * (prices.appsActiveVcpuSecond - prices.appsIdleVcpuSecond) * load.busySeconds;
  return {
    provider: "azure",
    providerName: "Azure",
    region: PRICED_REGION.azure,
    regionName: `Korea Central (${PRICED_REGION.azure})`,
    services: `Container Apps + PostgreSQL (${db.name})`,
    computeNote: `최소 복제본 ${prices.minReplicas}개 유지 + 요청 수수료`,
    cost: breakdown(
      idleCost + activeCost + (load.monthlyRequests / 1_000_000) * prices.requestsPerMillion,
      db.hour * HOURS_PER_MONTH + dbSizeGb * prices.pgStorageGbMonth,
      load.egressGb * prices.egressGb,
    ),
    carbon: CARBON_NOT_PROVIDED,
  };
}

function awsTargetProfile(params: FinOpsInputParams, load: Load, db: ResolvedDb): ProviderFinOpsProfile {
  const { cpuCount, memoryMb, dbSizeGb } = params;
  return {
    provider: "aws",
    providerName: "AWS",
    region: PRICED_REGION.aws,
    regionName: `Seoul (${PRICED_REGION.aws})`,
    services: `ECS Fargate + RDS (${db.name})`,
    computeNote: "상시 가동",
    cost: breakdown(
      (cpuCount * AWS_SEOUL.fargateVcpuHour + (memoryMb / 1024) * AWS_SEOUL.fargateGbHour) * HOURS_PER_MONTH,
      db.hour * HOURS_PER_MONTH + dbSizeGb * AWS_SEOUL.rdsStorageGbMonth,
      load.egressGb * AWS_SEOUL.egressGb,
    ),
    carbon: CARBON_NOT_PROVIDED,
  };
}

function tierCaveat(tierName: string): string {
  if (tierName === "db-g1-small") {
    return "db-g1-small 은 공유 코어로 Cloud SQL SLA 제외입니다 (Google Cloud SQL 인스턴스 설정 문서).";
  }
  if (tierName === "B1ms" || tierName === "B2s") {
    return "Burstable 은 운영 비권장입니다 (Microsoft Learn PostgreSQL 컴퓨팅 문서).";
  }
  return "";
}

/** 비교 기준의 조건 문구. 화면과 복사 보고서는 모두 이 함수의 결과만 씁니다. */
export function dbCondition({ basis, sourceDb, targetDb }: Pick<FinOpsComparisonResult, "basis" | "sourceDb" | "targetDb">): string {
  const src = sourceDb.memoryLabel;
  const tgt = targetDb.memoryLabel;
  let main = `DB 메모리는 원본 이상입니다 (${src} → ${tgt}). 사용량 근거 미제공.`;
  if (basis === "same_memory") {
    main = `메모리만 맞춘 비교입니다. 원본 ${sourceDb.cpuLabel} ${src}, 목적지 ${targetDb.cpuLabel} ${tgt}.`;
  } else if (targetDb.memoryGb < sourceDb.memoryGb) {
    main = `DB 메모리를 ${src} 에서 ${tgt} 로 줄인 경우입니다. 사용량 근거 미제공.`;
  }
  return [main, tierCaveat(targetDb.name)].filter(Boolean).join(" ");
}

function compareOn(params: FinOpsInputParams, db: ResolvedDb): FinOpsComparisonResult {
  const load = loadOf(params);
  const source = awsProfile(params, load);
  const target =
    params.targetProvider === "gcp"
      ? gcpProfile(params, load, db)
      : params.targetProvider === "azure"
        ? azureProfile(params, load, db)
        : awsTargetProfile(params, load, db);
  const { memoryLabel, memoryGb, cpuLabel } = DB_CLASSES[params.dbClass];
  const facts = {
    basis: params.dbBasis,
    sourceDb: { name: params.dbClass, memoryLabel, memoryGb, cpuLabel },
    targetDb: { name: db.name, memoryLabel: db.memoryLabel, memoryGb: db.memoryGb, cpuLabel: db.cpuLabel, tierOrigin: db.tierOrigin },
  };

  const deltaUsd = target.cost.total - source.cost.total;
  const deltaPercent = (deltaUsd / source.cost.total) * 100;
  const { verdict, reason } = judge(deltaUsd, deltaPercent, db.memoryGb < memoryGb);

  return {
    source,
    target,
    monthlyDeltaUsd: roundUsd(deltaUsd),
    deltaPercent: Number(deltaPercent.toFixed(1)),
    annualDeltaUsd: roundUsd(deltaUsd * 12),
    verdict,
    verdictReason: reason,
    ...facts,
    condition: dbCondition(facts),
  };
}

/** 같은 부하를 AWS 와 목적지에 올렸을 때의 월 비용을 공시가로 추정합니다. 등급이 가격표에 없으면 null 입니다. */
export function computeFinOpsComparison(params: FinOpsInputParams & { dbBasis: "same_memory" }): FinOpsComparisonResult;
export function computeFinOpsComparison(params: FinOpsInputParams): FinOpsComparisonResult | null;
export function computeFinOpsComparison(params: FinOpsInputParams): FinOpsComparisonResult | null {
  const db = resolveTargetDb(params);
  return db && compareOn(params, db);
}

export function computeBothBases(params: FinOpsInputParams): BothBases {
  return {
    sameMemory: computeFinOpsComparison({ ...params, dbBasis: "same_memory" }),
    asDeployed: computeFinOpsComparison({ ...params, dbBasis: "as_deployed" }),
  };
}

/** 금액, 증감률, 조건을 한 줄로 묶습니다. 화면과 복사 보고서가 함께 씁니다. */
export function basisSummaryLine(result: FinOpsComparisonResult): string {
  const label = result.basis === "same_memory" ? "같은 메모리 등급" : "실제 배포 등급";
  const { name, tierOrigin } = result.targetDb;
  return `${label} ${name} (${tierOrigin}): ${signedPercent(result.deltaPercent)}, 월 ${signedUsd(result.monthlyDeltaUsd)}. ${result.condition}`;
}

export interface FlipPoint {
  axis: FlipAxis;
  between: [number, number] | null;
}

function sliderValues({ min, max, step }: { min: number; max: number; step: number }): number[] {
  return Array.from({ length: Math.floor((max - min) / step) + 1 }, (_, index) => min + index * step);
}

function firstSignChange(values: number[], deltaAt: (value: number) => number): [number, number] | null {
  let prevVal: number | null = null;
  let prevSign: number | null = null;

  for (const value of values) {
    const delta = deltaAt(value);
    const sign = Math.sign(delta);
    if (sign === 0) continue;

    if (prevSign !== null && sign !== prevSign && prevVal !== null) {
      return [prevVal, value];
    }
    prevVal = value;
    prevSign = sign;
  }
  return null;
}

/** 슬라이더 값을 차례로 넣어 증감 방향이 처음 바뀌는 구간을 찾습니다. 등급이 가격표에 없으면 빈 배열입니다. */
export function findFlipPoints(params: FinOpsInputParams): FlipPoint[] {
  const db = resolveTargetDb(params);
  if (!db) return [];
  return (Object.keys(SLIDER_RANGES) as FlipAxis[]).map((axis) => ({
    axis,
    between: firstSignChange(sliderValues(SLIDER_RANGES[axis]), (value) => compareOn({ ...params, [axis]: value }, db).monthlyDeltaUsd),
  }));
}

export function flipPointLine({ axis, between }: FlipPoint): string {
  const name = FLIP_AXIS_LABEL[axis];
  return between
    ? `${name}: ${between[0]} → ${between[1]} 사이에서 AWS 대비 증감 방향이 바뀝니다`
    : `${name}: 이 범위에서는 뒤집히지 않음`;
}

export interface SensitivityRow {
  label: string;
  sourceTotal: number;
  targetTotal: number;
  monthlyDeltaUsd: number;
  deltaPercent: number;
}

const SENSITIVITY_RPS = [1, 10, 100];
// Fargate 의 가장 작은 공시 조합입니다.
const SMALLEST_AWS_APP = { sourceCpuCount: 0.25, sourceMemoryMb: 512 };

/** 같은 메모리 등급 기준으로 입력 하나만 바꾼 값을 냅니다. 다른 입력은 현재값입니다. */
export function computeSensitivity(params: FinOpsInputParams): SensitivityRow[] {
  const cases: { label: string; change: Partial<FinOpsInputParams> }[] = [
    ...SENSITIVITY_RPS.map((rps) => ({ label: `RPS ${rps}`, change: { rps } })),
    ...(Object.keys(DB_CLASSES) as DbClass[]).map((dbClass) => ({ label: `원본 DB ${dbClass}`, change: { dbClass } })),
    { label: `AWS 앱 ${SMALLEST_AWS_APP.sourceCpuCount} vCPU / ${SMALLEST_AWS_APP.sourceMemoryMb} MiB`, change: SMALLEST_AWS_APP },
  ];
  return cases.map(({ label, change }) => {
    const result = computeFinOpsComparison({ ...params, ...change, dbBasis: "same_memory" });
    return {
      label,
      sourceTotal: result.source.cost.total,
      targetTotal: result.target.cost.total,
      monthlyDeltaUsd: result.monthlyDeltaUsd,
      deltaPercent: result.deltaPercent,
    };
  });
}

export interface CostReportInput {
  params: FinOpsInputParams;
  origins: { targetApp: InputOrigin; disk: InputOrigin };
}

const sliderOrigin = (value: unknown, defaultValue: unknown): InputOrigin => (value === defaultValue ? "가정" : "사용자 입력");

function reportInputsLine({ params, origins }: CostReportInput): string {
  const { rps, idlePercent, avgRequestMs, dbClass, dbSizeGb } = params;
  return [
    `RPS ${rps} (${sliderOrigin(rps, FINOPS_DEFAULTS.rps)})`,
    `유휴 ${idlePercent}% (${sliderOrigin(idlePercent, FINOPS_DEFAULTS.idlePercent)})`,
    `요청당 ${avgRequestMs}ms (${sliderOrigin(avgRequestMs, FINOPS_DEFAULTS.avgRequestMs)})`,
    `원본 앱 ${params.sourceCpuCount} vCPU / ${params.sourceMemoryMb} MiB (가정)`,
    `목적지 앱 ${params.cpuCount} vCPU / ${params.memoryMb} MiB (${origins.targetApp})`,
    `원본 DB ${dbClass} (${sliderOrigin(dbClass, FINOPS_DEFAULTS.dbClass)})`,
    `디스크 ${dbSizeGb}GB (${origins.disk})`,
  ].join(", ");
}

/** 복사용 보고서. 판정은 같은 메모리 기준이고, 실제 배포 등급의 수치는 조건과 같은 줄에만 씁니다. */
export function buildCostReportText(input: CostReportInput): string {
  const { sameMemory, asDeployed } = computeBothBases(input.params);
  const { source, target } = sameMemory;
  return [
    "[CloudMorph] 멀티클라우드 월 비용 추정 (공시가 기준, 실측 아님)",
    `· 이전: ${source.providerName} ${source.regionName} → ${target.providerName} ${target.regionName}`,
    `· 월 비용 (같은 메모리 등급): ${formatUsd(source.cost.total)} → ${formatUsd(target.cost.total)}`,
    `· ${basisSummaryLine(sameMemory)}`,
    `· ${asDeployed ? basisSummaryLine(asDeployed) : `실제 배포 등급: ${NOT_PROVIDED}`}`,
    `· 판정 (같은 메모리 등급): ${VERDICT_LABEL[sameMemory.verdict]}. ${sameMemory.verdictReason}`,
    `· 입력: ${reportInputsLine(input)}`,
    `· ${RIGHTSIZING_NOT_PROVIDED}`,
    `· 계산 제외: ${EXCLUDED_COSTS.join(", ")}`,
    `· 고정 가정: 요청당 응답 ${RESPONSE_KB_PER_REQUEST}KB, AWS·GCP 인스턴스 1개, Azure 최소 복제본 ${AZURE_KOREA_CENTRAL.minReplicas}개, Cloud Run 요청 기반 과금`,
    `· 공시가 확인일: ${PRICE_AS_OF}`,
  ].join("\n");
}

/** 가격표에 없는 목적지면 안내 문구를 돌려줍니다. provider 가 없으면 백엔드 기본값과 같이 gcp 로 봅니다. */
export function unsupportedTargetMessage(provider: string | null | undefined, region: string | null | undefined): string | null {
  const name = provider ?? "gcp";
  const isPriced =
    (name === "gcp" || name === "azure" || name === "aws") && (!region || region === PRICED_REGION[name]);
  return isPriced ? null : `이 run 의 목적지(${name} ${region || NOT_PROVIDED})는 가격표에 없어 비용 추정을 표시하지 않습니다 (미제공).`;
}
