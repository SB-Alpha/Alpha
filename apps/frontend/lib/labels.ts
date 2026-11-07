import type { CutoverSecondsSource } from "./dashboard";
import type { Gate, PreflightVerdict, StageName, WorkflowState } from "./workflows";

export const STAGE_LABEL: Record<StageName, string> = {
  discovery: "원본 조사",
  planning: "이전 계획",
  provisioning: "목적지 구축",
  db_migration: "DB 동기화",
  app_deploy: "앱 배포",
  cutover: "트래픽 전환",
};

export const GATE_LABEL: Record<Gate, string> = { plan: "승인 ① 계획", cutover: "승인 ② 전환" };

export const PREFLIGHT_VERDICT_LABEL: Record<PreflightVerdict, string> = {
  go: "필수 항목 모두 통과",
  no_go: "실패 항목 있음",
  unverified: "미확인 항목 있음",
};

export const CUTOVER_SECONDS_SOURCE_LABEL: Record<CutoverSecondsSource, string> = {
  worker: "Worker 기준",
  agent: "에이전트 기준",
};

export const STATE_LABEL: Record<WorkflowState, string> = {
  pending: "대기",
  running: "실행 중",
  waiting_approval: "승인 대기",
  paused: "멈춤",
  succeeded: "완료",
  failed: "실패",
  aborted: "중단됨",
};

// 상태 색은 의미로만 씁니다: 진행(파랑) · 사람 필요(호박) · 완료(초록) · 실패(빨강) · 비활성(회색)
export const STATE_TONE: Record<WorkflowState, string> = {
  pending: "bg-stone-100 text-stone-600 ring-stone-300",
  running: "bg-sky-50 text-sky-800 ring-sky-300",
  waiting_approval: "bg-amber-50 text-amber-900 ring-amber-400",
  paused: "bg-amber-50 text-amber-900 ring-amber-300",
  succeeded: "bg-emerald-50 text-emerald-800 ring-emerald-300",
  failed: "bg-rose-50 text-rose-800 ring-rose-300",
  aborted: "bg-stone-200 text-stone-700 ring-stone-400",
};

export function formatTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "medium" });
}
