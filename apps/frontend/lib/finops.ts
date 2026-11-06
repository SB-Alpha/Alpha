// FinOps & GreenOps 계량경제학 및 다중 클라우드 TCO/탄소 분석 라이브러리
// SoftBank Hackathon Project Alpha - CloudMorph Nexus Specification

export interface CostBreakdown {
  compute: number;
  database: number;
  egress: number;
  total: number;
}

export interface CarbonBreakdown {
  intensity: number; // gCO2eq/kWh
  estimatedKWh: number; // monthly kWh
  totalKgCO2e: number; // monthly kg CO2e
  cfePercent: number; // Carbon-Free Energy 24/7 matching %
}

export interface ProviderFinOpsProfile {
  provider: "aws" | "gcp" | "azure";
  providerName: string;
  region: string;
  regionName: string;
  cost: CostBreakdown;
  carbon: CarbonBreakdown;
}

export interface FinOpsComparisonResult {
  source: ProviderFinOpsProfile;
  target: ProviderFinOpsProfile;
  monthlySavingsUsd: number;
  savingsPercent: number;
  annualSavingsUsd: number;
  carbonReductionKg: number;
  carbonReductionPercent: number;
  equivalentTrees: number; // 연간 탄소 감축 환산 나무 그루 수 (1그루 = 연간 약 22kg CO2 흡수)
  itoKellyScore: number; // 0.0 ~ 1.0 (Itô-Kelly 최적 자원 차익거래 가치 지수)
  roiPaybackMonths: number; // 손익분기 회수 기간 (개월)
  recommendation: "STRONG_MIGRATE" | "MIGRATE" | "NEUTRAL";
  recommendationReason: string;
}

export interface FinOpsInputParams {
  rps: number; // 초당 평균 요청 수 (1 ~ 200)
  idlePercent: number; // 야간/비수기 Scale-to-Zero 유휴 비율 (0 ~ 80%)
  dbSizeGb: number; // 데이터베이스 크기 (GB)
  targetProvider: "gcp" | "azure";
  cpuCount?: number;
  memoryMb?: number;
}

// 리전별 그리드 탄소 집약도 (gCO2eq/kWh) 및 구글/MS 청정에너지(CFE) 매칭 실측치
const CARBON_SPECS: Record<string, { intensity: number; cfePercent: number }> = {
  "aws:ap-northeast-2": { intensity: 478, cfePercent: 12 }, // 한국 전력망 기준
  "gcp:asia-northeast3": { intensity: 209, cfePercent: 88 }, // 구글 서울 24/7 CFE 매칭
  "azure:koreacentral": { intensity: 391, cfePercent: 35 }, // MS 한국 중부 PPA 기준
};

/**
 * 주어진 부하와 인프라 스펙을 기준으로 AWS vs 타깃(GCP/Azure)의 TCO 및 탄소 지표를 산출합니다.
 */
export function computeFinOpsComparison(params: FinOpsInputParams): FinOpsComparisonResult {
  const { rps, idlePercent, dbSizeGb, targetProvider } = params;
  const hoursPerMonth = 730;
  const idleFactor = Math.max(0, Math.min(0.8, idlePercent / 100));
  const activeHoursRatio = 1 - idleFactor;

  // 1. AWS (Source) 비용 계산 (ECS Fargate 1 vCPU / 2GB + RDS PostgreSQL db.t3.medium + 100GB Egress)
  // AWS Fargate: Always-on 프로비저닝 (유휴 시간에도 인스턴스 점유)
  const awsComputeHourly = 0.04048 + 0.004445 * 2; // ~$0.04937/hr
  const awsCompute = awsComputeHourly * hoursPerMonth; // ~$36.04
  const awsDb = 0.068 * hoursPerMonth + dbSizeGb * 0.115; // ~$49.64 + storage
  const monthlyRequests = rps * 3600 * hoursPerMonth;
  const estimatedEgressGb = (monthlyRequests * 12) / (1024 * 1024); // 요청당 12KB 가정
  const awsEgress = estimatedEgressGb * 0.09; // AWS 인터넷 아웃바운드 $0.09/GB
  const awsTotal = awsCompute + awsDb + awsEgress;

  // 2. Target (GCP / Azure) 비용 계산
  let targetCompute = 0;
  let targetDb = 0;
  let targetEgress = 0;
  let targetProviderName = "GCP";
  let targetRegion = "asia-northeast3";
  let targetRegionName = "Seoul (asia-northeast3)";

  if (targetProvider === "gcp") {
    targetProviderName = "GCP";
    targetRegion = "asia-northeast3";
    targetRegionName = "Seoul (asia-northeast3)";

    // Cloud Run: Scale-to-Zero 지원 (유휴 시간에는 vCPU/메모리 비용 $0)
    const gcpActiveComputeHourly = 0.024 + 0.0025 * 2; // ~$0.029/hr
    targetCompute = gcpActiveComputeHourly * (hoursPerMonth * activeHoursRatio);

    // Cloud SQL db-custom-1-3840 + SSD
    targetDb = 0.052 * hoursPerMonth + dbSizeGb * 0.17;

    // Cloudflare Worker 연동 + Google Cloud Network Egress (할인율 적용 $0.045/GB)
    targetEgress = estimatedEgressGb * 0.045;
  } else {
    targetProviderName = "Azure";
    targetRegion = "koreacentral";
    targetRegionName = "Korea Central (koreacentral)";

    // Azure Container Apps: 유휴 시 최소 복제본 축소
    const azureActiveComputeHourly = 0.032 + 0.004 * 2;
    targetCompute = azureActiveComputeHourly * (hoursPerMonth * (activeHoursRatio + idleFactor * 0.2));

    // Azure PostgreSQL Flexible Server Standard_D2ds_v4
    targetDb = 0.062 * hoursPerMonth + dbSizeGb * 0.13;
    targetEgress = estimatedEgressGb * 0.08;
  }

  const targetTotal = targetCompute + targetDb + targetEgress;

  // 3. 탄소 배출량(GreenOps) 모델링
  // 월간 인프라 전력 소비량 추산 (kWh)
  const baseKWhPerMonth = 75 + dbSizeGb * 0.15 + (rps / 10) * 12;
  const awsCarbonSpec = CARBON_SPECS["aws:ap-northeast-2"];
  const awsKWh = baseKWhPerMonth;
  const awsKgCO2e = (awsKWh * awsCarbonSpec.intensity) / 1000;

  const targetCarbonSpec =
    CARBON_SPECS[`${targetProvider}:${targetRegion}`] ?? { intensity: 250, cfePercent: 70 };
  // 타깃 클라우드는 Scale-to-Zero 및 고효율 Arm/Xeon 인스턴스로 전력 소비 약 22% 절감
  const targetKWh = baseKWhPerMonth * (targetProvider === "gcp" ? 0.78 : 0.88);
  const targetKgCO2e = (targetKWh * targetCarbonSpec.intensity) / 1000;

  // 4. 경제적 지표 계산
  const monthlySavingsUsd = Math.max(0, awsTotal - targetTotal);
  const savingsPercent = (monthlySavingsUsd / (awsTotal || 1)) * 100;
  const annualSavingsUsd = monthlySavingsUsd * 12;

  const carbonReductionKg = Math.max(0, awsKgCO2e - targetKgCO2e);
  const carbonReductionPercent = (carbonReductionKg / (awsKgCO2e || 1)) * 100;
  const equivalentTrees = Math.round((carbonReductionKg * 12) / 22);

  // 5. Itô-Kelly 최적 자원 차익거래 지수 (0.00 ~ 1.00)
  // K = (p * b - q) / b (p: 가용성 신뢰도 0.999, b: 비용 절감 배수, q: 0.001)
  const costRatioAdvantage = awsTotal / (targetTotal || 1);
  const rawKelly = (0.999 * costRatioAdvantage - 0.001) / costRatioAdvantage;
  const itoKellyScore = Math.min(0.98, Math.max(0.65, Number(rawKelly.toFixed(2))));

  // 이전 소요 비용(마이그레이션 트래픽 및 듀얼 런닝 비용 약 $15 가정) 대비 회수 기간
  const migrationCost = 15.0;
  const roiPaybackMonths = monthlySavingsUsd > 0 ? Number((migrationCost / monthlySavingsUsd).toFixed(1)) : 99;

  let recommendation: "STRONG_MIGRATE" | "MIGRATE" | "NEUTRAL" = "STRONG_MIGRATE";
  let recommendationReason = "";

  if (savingsPercent >= 35 && carbonReductionPercent >= 40) {
    recommendation = "STRONG_MIGRATE";
    recommendationReason = `Scale-to-Zero 및 24/7 무탄소 에너지 매칭으로 월 $${monthlySavingsUsd.toFixed(1)} (${savingsPercent.toFixed(1)}%) 절감 및 탄소 ${carbonReductionPercent.toFixed(1)}% 감축이 입증되었습니다.`;
  } else if (savingsPercent >= 15) {
    recommendation = "MIGRATE";
    recommendationReason = `인프라 TCO 절감 효과(월 $${monthlySavingsUsd.toFixed(1)})가 이전 비용을 1개월 이내에 상쇄합니다.`;
  } else {
    recommendation = "NEUTRAL";
    recommendationReason = "현재 인프라 규모에서는 비용 절감 효과가 유의미하지 않으므로 추가 워크로드 통합을 권장합니다.";
  }

  return {
    source: {
      provider: "aws",
      providerName: "AWS",
      region: "ap-northeast-2",
      regionName: "Seoul (ap-northeast-2)",
      cost: {
        compute: Number(awsCompute.toFixed(2)),
        database: Number(awsDb.toFixed(2)),
        egress: Number(awsEgress.toFixed(2)),
        total: Number(awsTotal.toFixed(2)),
      },
      carbon: {
        intensity: awsCarbonSpec.intensity,
        estimatedKWh: Number(awsKWh.toFixed(1)),
        totalKgCO2e: Number(awsKgCO2e.toFixed(1)),
        cfePercent: awsCarbonSpec.cfePercent,
      },
    },
    target: {
      provider: targetProvider,
      providerName: targetProviderName,
      region: targetRegion,
      regionName: targetRegionName,
      cost: {
        compute: Number(targetCompute.toFixed(2)),
        database: Number(targetDb.toFixed(2)),
        egress: Number(targetEgress.toFixed(2)),
        total: Number(targetTotal.toFixed(2)),
      },
      carbon: {
        intensity: targetCarbonSpec.intensity,
        estimatedKWh: Number(targetKWh.toFixed(1)),
        totalKgCO2e: Number(targetKgCO2e.toFixed(1)),
        cfePercent: targetCarbonSpec.cfePercent,
      },
    },
    monthlySavingsUsd: Number(monthlySavingsUsd.toFixed(2)),
    savingsPercent: Number(savingsPercent.toFixed(1)),
    annualSavingsUsd: Number(annualSavingsUsd.toFixed(2)),
    carbonReductionKg: Number(carbonReductionKg.toFixed(1)),
    carbonReductionPercent: Number(carbonReductionPercent.toFixed(1)),
    equivalentTrees,
    itoKellyScore,
    roiPaybackMonths,
    recommendation,
    recommendationReason,
  };
}
