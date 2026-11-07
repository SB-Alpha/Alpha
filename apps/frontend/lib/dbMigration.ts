// CDC 에이전트 작업 직접 제어 (backend `/db-migration/jobs`). run 의 상태 머신을 거치지 않는 비상용입니다.

import { request } from "./workflows";

const BASE = "/api/db-migration/jobs";

// 에이전트 작업의 message 에는 접속 정보가 들어갈 수 있어 상태와 단계만 씁니다.
export interface CdcJobSummary {
  state: string;
  phase: string | null;
}

const summarize = ({ state, phase }: CdcJobSummary): CdcJobSummary => ({ state, phase });

const jobPath = (jobId: string) => `${BASE}/${encodeURIComponent(jobId)}`;

const postAction = async (jobId: string, action: "retry" | "close" | "abort") =>
  summarize(await request<CdcJobSummary>(`${jobPath(jobId)}/${action}`, { method: "POST" }));

export const dbMigration = {
  getJob: async (jobId: string) => summarize(await request<CdcJobSummary>(jobPath(jobId))),
  retry: (jobId: string) => postAction(jobId, "retry"),
  close: (jobId: string) => postAction(jobId, "close"),
  abort: (jobId: string) => postAction(jobId, "abort"),
};

// 아래 조건은 에이전트 cdc_agent/main.py 의 규칙과 같습니다. 어긋나도 에이전트가 409 로 거절합니다.
const RETRY_PHASES = new Set(["source_prepare", "target_prepare", "precheck", "replication_setup", "snapshot", "streaming"]);
const PAST_CUTOVER_PHASES = new Set(["cutover", "stabilizing", "closed"]);

export const canRetry = (job: CdcJobSummary) => job.state === "failed" && RETRY_PHASES.has(job.phase ?? "");
export const canClose = (job: CdcJobSummary) => job.phase === "stabilizing";
export const canAbort = (job: CdcJobSummary) =>
  job.state !== "aborted" && job.state !== "succeeded" && !PAST_CUTOVER_PHASES.has(job.phase ?? "");
