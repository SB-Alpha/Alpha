// 승인 ① 화면이 쓰는 planning 결과. 필드는 planning PR #21 `MigrationPlan` 기준입니다.
// 수동 입력 등으로 모양이 다르면 null / "미제공" 으로 둡니다 (값을 지어내지 않음).

export type CheckResult = "pass" | "fail" | "unknown";

export interface PlanCheck {
  id: string;
  title: string;
  result: CheckResult;
  detail: string;
}

export interface PlanRisk {
  level: "high" | "medium" | "low";
  title: string;
  detail: string;
}

export interface PlanView {
  summary: string[];
  checks: PlanCheck[];
  risks: PlanRisk[];
  approvable: boolean | null; // null = planning 이 값을 주지 않음
  blocking: string[];
  summarySource: "rule" | "ai" | null;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((s): s is string => typeof s === "string") : []);

export function readPlan(output: unknown): PlanView | null {
  if (!isRecord(output)) return null;
  const root = isRecord(output.plan) ? output.plan : output;
  const checks = Array.isArray(root.checks)
    ? root.checks.filter(isRecord).map((c): PlanCheck => ({
        id: String(c.id ?? ""),
        title: String(c.title ?? c.id ?? "이름 없음"),
        result: c.result === "pass" || c.result === "fail" ? c.result : "unknown",
        detail: typeof c.detail === "string" ? c.detail : "",
      }))
    : [];
  const risks = Array.isArray(root.risks)
    ? root.risks.filter(isRecord).map((r): PlanRisk => ({
        level: r.level === "high" || r.level === "low" ? r.level : "medium",
        title: String(r.title ?? ""),
        detail: typeof r.detail === "string" ? r.detail : "",
      }))
    : [];
  return {
    summary: strings(root.summary),
    checks,
    risks,
    approvable: typeof root.approvable === "boolean" ? root.approvable : null,
    blocking: strings(root.blocking),
    summarySource: root.summary_source === "ai" || root.summary_source === "rule" ? root.summary_source : null,
  };
}

/**
 * 승인 ① 버튼을 열지 정합니다. 팀 합의 전 기본값:
 * - planning 이 approvable=false 를 주면 막습니다 (필수 조건 미통과).
 * - 값이 없으면(수동 입력 등) 열어 두되 운영자가 직접 확인하라고 알립니다.
 */
export function canApprovePlan(plan: PlanView | null): { allowed: boolean; reason: string | null } {
  if (plan?.approvable === false) {
    return { allowed: false, reason: "필수 조건을 통과하지 못해 승인할 수 없습니다." };
  }
  if (plan === null || plan.approvable === null) {
    return { allowed: true, reason: "planning 결과가 미제공입니다. 수동 입력 내용을 직접 확인한 뒤 승인하세요." };
  }
  return { allowed: true, reason: null };
}
