// FinOps 화면 공통 값. 금액·교차점·차익거래는 모두 백엔드(/finops/estimate)가 가격 API 단가로 냅니다.
// 화면은 단가표나 계산식을 갖지 않고, 모르는 값은 "미제공" 으로 둡니다.

export type TargetProvider = "gcp" | "azure" | "aws";

export const NOT_PROVIDED = "미제공";

export const CLOUD_LABEL: Record<string, string> = { aws: "AWS", gcp: "GCP", azure: "Azure" };

export const cloudLabel = (cloud: string | null | undefined): string =>
  cloud ? (CLOUD_LABEL[cloud] ?? cloud) : NOT_PROVIDED;

export const formatUsd = (value: number): string =>
  `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
