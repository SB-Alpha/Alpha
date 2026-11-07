// 3D 시각화 컴포넌트들이 사용하는 실제 run 데이터 추출기입니다.
// 화면 규칙: 모듈이 값을 주지 않으면 null("미제공")을 반환하며, 임의의 0이나 성공 값으로 채우지 않습니다.

import {
  METRIC_KEYS,
  cutoverApprovals,
  cutoverSecondsSource,
  cutoverSequence,
  cutoverSteps,
  findMetric,
  formatBytes,
  numberMetric,
  overallProgress,
  SERVING_LABEL,
  servingSide,
  tableRowTotals,
} from "./dashboard";
import { STAGE_LABEL, formatTime } from "./labels";
import { isRecord, readPlan } from "./plan";
import { runTitle, targetSummary, type RunStatus, type StageName, type WorkflowState } from "./workflows";

export interface TableSyncItem {
  name: string;
  sourceRows: number;
  targetRows: number;
  percent: number | null;
  match: boolean;
}

export interface SecurityAlertItem {
  id: string;
  provider: "aws" | "gcp" | "azure" | "onprem";
  severity: "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
  resourceType: string;
  resourceId: string;
  threatCategory: string;
  description: string;
}

export interface BlastRadiusInfo {
  isCompromised: boolean;
  quarantined: boolean;
  riskScore: number;
  hopDistance: number;
  alert: SecurityAlertItem | null;
}

export interface SourceNodeData {
  serviceTitle: string;
  /** 요청의 원본 종류 (AWS·GCP·Azure·수동) */
  cloudName: string;
  region: string | null;
  ecsService: string | null;
  rdsInstanceId: string | null;
  dbVersion: string | null;
  vpcId: string | null;
  sourceRows: number | null;
  isServing: boolean;
  blastRadius?: BlastRadiusInfo | null;
}

export interface CdcArcData {
  stageState: WorkflowState;
  copyPercent: number | null;
  lagBytes: number | null;
  lagFormatted: string | null;
  mismatchCount: number | null;
  cdcStatus: string | null;
  cdcJobId: string | null;
  tables: TableSyncItem[];
}

export interface TargetNodeData {
  providerSummary: string;
  region: string | null;
  vpcId: string | null;
  dbInstanceId: string | null;
  cloudSqlIp: string | null;
  dbName: string | null;
  cloudRunUrl: string | null;
  /** app_deploy 가 실제로 쳐 본 헬스 체크 결과. 보고가 없으면 null */
  healthy: boolean | null;
  cpuMemory: string | null;
  targetRows: number | null;
  isServing: boolean;
}

export interface StageReportCardData {
  index: number;
  stage: StageName;
  label: string;
  state: WorkflowState;
  progress: number;
  manual: boolean;
  message: string | null;
  updatedAt: string | null;
  facts: { label: string; value: string; tone?: "good" | "bad" | "warn" | "neutral" }[];
}

function strOrNull(v: unknown): string | null {
  return typeof v === "string" && v.trim() !== "" ? v.trim() : null;
}

function numOrNull(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export function extractTables(run: RunStatus): TableSyncItem[] {
  const raw = findMetric(run, METRIC_KEYS.tables);
  if (!isRecord(raw)) return [];
  const items: TableSyncItem[] = [];
  for (const [name, val] of Object.entries(raw)) {
    if (!isRecord(val)) continue;
    const sourceRows = numOrNull(val.source_rows);
    const targetRows = numOrNull(val.target_rows);
    if (sourceRows === null || targetRows === null) continue;
    const percent =
      numOrNull(val.percent) ?? (sourceRows > 0 ? Math.round((targetRows / sourceRows) * 100) : null);
    const match = typeof val.match === "boolean" ? val.match : sourceRows === targetRows;
    items.push({ name, sourceRows, targetRows, percent, match });
  }
  return items;
}

const SOURCE_CLOUD_NAME: Record<string, string> = { aws: "AWS", gcp: "GCP", azure: "Azure", manual: "수동 입력" };

export function extractSourceNodeData(run: RunStatus): SourceNodeData {
  const planningOut = isRecord(run.outputs.planning) ? run.outputs.planning : null;
  const sourceOut = planningOut && isRecord(planningOut.source) ? planningOut.source : null;
  const dbOut = sourceOut && isRecord(sourceOut.db) ? sourceOut.db : null;
  const netOut = sourceOut && isRecord(sourceOut.network) ? sourceOut.network : null;
  const rows = tableRowTotals(run);
  const serving = servingSide(run);

  return {
    serviceTitle: runTitle(run.request),
    cloudName: SOURCE_CLOUD_NAME[run.request.source_kind ?? "aws"],
    // AWS 원본은 요청의 aws_region, GCP·Azure 원본은 cloud_source.region, 둘 다 없으면 assessment 결과를 씁니다.
    region:
      strOrNull(run.request.aws_region) ?? strOrNull(run.request.cloud_source?.region) ?? strOrNull(sourceOut?.region),
    ecsService: strOrNull(run.request.ecs_service) ?? strOrNull(run.request.service_name),
    rdsInstanceId: strOrNull(run.request.rds_instance_id) ?? strOrNull(dbOut?.instance_id),
    dbVersion: strOrNull(dbOut?.version),
    vpcId: strOrNull(netOut?.vpc_id),
    sourceRows: rows?.source ?? null,
    isServing: serving === "source",
  };
}

export function extractCdcArcData(run: RunStatus): CdcArcData {
  const dbStage = run.stages.find((s) => s.stage === "db_migration");
  const dbOut = isRecord(run.outputs.db_migration) ? run.outputs.db_migration : null;
  const cutoverOut = isRecord(run.outputs.cutover) ? run.outputs.cutover : null;

  // runner 출력은 {job_id, phase, state, metrics} 입니다 (workflows/runners.py run_db_migration).
  const dbMetrics = dbOut && isRecord(dbOut.metrics) ? dbOut.metrics : null;
  const copyPercent = numberMetric(run, METRIC_KEYS.copyPercent) ?? numOrNull(dbMetrics?.copy_percent);
  const lagBytes =
    numberMetric(run, METRIC_KEYS.replicationLagBytes) ?? numOrNull(dbMetrics?.replication_lag_bytes);
  const mismatchCount = numberMetric(run, METRIC_KEYS.mismatchTables);

  return {
    stageState: dbStage?.state ?? "pending",
    copyPercent,
    lagBytes,
    lagFormatted: lagBytes !== null ? formatBytes(lagBytes) : null,
    mismatchCount,
    cdcStatus: strOrNull(dbOut?.state) ?? strOrNull(dbOut?.phase),
    cdcJobId: strOrNull(run.cdc_job_id) ?? strOrNull(cutoverOut?.job_id),
    tables: extractTables(run),
  };
}

export function extractTargetNodeData(run: RunStatus): TargetNodeData {
  const provOut = isRecord(run.outputs.provisioning) ? run.outputs.provisioning : null;
  const provTarget = isRecord(provOut?.target) ? provOut.target : null;
  const provNetwork = isRecord(provTarget?.network) ? provTarget.network : null;
  const provDb = isRecord(provTarget?.db) ? provTarget.db : null;
  const provApp = isRecord(provTarget?.app) ? provTarget.app : null;

  const deployOut = isRecord(run.outputs.app_deploy) ? run.outputs.app_deploy : null;
  const deployTarget = isRecord(deployOut?.target) ? deployOut.target : null;
  const deployApp = isRecord(deployTarget?.app) ? deployTarget.app : null;

  const planningOut = isRecord(run.outputs.planning) ? run.outputs.planning : null;
  const planObj = planningOut && isRecord(planningOut.plan) ? planningOut.plan : null;
  const targetInfra = planObj && isRecord(planObj.target_infra) ? planObj.target_infra : null;

  const cpu = numOrNull(targetInfra?.cpu);
  const memory = numOrNull(targetInfra?.memory);
  const rows = tableRowTotals(run);
  const serving = servingSide(run);

  return {
    providerSummary: targetSummary(run.request),
    region: strOrNull(run.request.target_region) ?? strOrNull(provTarget?.region) ?? strOrNull(targetInfra?.region),
    vpcId: strOrNull(provNetwork?.vpc_name),
    dbInstanceId: strOrNull(provDb?.instance),
    cloudSqlIp: strOrNull(provDb?.private_ip),
    dbName: strOrNull(provDb?.database) ?? strOrNull(targetInfra?.db_name),
    cloudRunUrl: strOrNull(deployApp?.url) ?? strOrNull(provApp?.url) ?? strOrNull(deployOut?.app_url),
    healthy: typeof deployOut?.healthy === "boolean" ? deployOut.healthy : null,
    cpuMemory: cpu !== null && memory !== null ? `${cpu} vCPU / ${memory} MiB` : null,
    targetRows: rows?.target ?? null,
    isServing: serving === "target",
  };
}

export function extractStageReports(run: RunStatus): StageReportCardData[] {
  const source = extractSourceNodeData(run);
  const cdc = extractCdcArcData(run);
  const target = extractTargetNodeData(run);
  const plan = readPlan(run.outputs.planning);
  const planApproval = (run.approval_records ?? []).find((r) => r.gate === "plan");
  const cutoverApprovalList = cutoverApprovals(run);
  const lastCutoverApproval = cutoverApprovalList[cutoverApprovalList.length - 1] ?? null;
  const cutoverSec = numberMetric(run, METRIC_KEYS.cutoverSeconds);
  const cutoverSecSrc = cutoverSecondsSource(run);
  const sequence = cutoverSequence(run);
  const steps = cutoverSteps(run);
  const serving = servingSide(run);
  const failedReqs = numberMetric(run, METRIC_KEYS.failedRequests);

  return run.stages.map((s, idx): StageReportCardData => {
    const facts: StageReportCardData["facts"] = [];

    if (s.stage === "discovery") {
      facts.push({ label: "원본 리전", value: source.region ?? "미제공" });
      facts.push({ label: "대상 서비스", value: source.ecsService ?? "미제공" });
      facts.push({ label: "RDS 인스턴스", value: source.rdsInstanceId ?? "미제공" });
      facts.push({
        label: "원본 행 수 합계",
        value: source.sourceRows !== null ? `${source.sourceRows.toLocaleString("ko-KR")}행` : "미제공",
      });
    } else if (s.stage === "planning") {
      const passCount = plan ? plan.checks.filter((c) => c.result === "pass").length : null;
      const totalChecks = plan ? plan.checks.length : null;
      facts.push({
        label: "필수 조건 검사",
        value: passCount !== null && totalChecks !== null ? `${passCount}/${totalChecks} 통과` : "미제공",
        tone: passCount !== null && passCount === totalChecks && totalChecks > 0 ? "good" : "warn",
      });
      facts.push({
        label: "목적지 사양",
        value: target.cpuMemory ?? "미제공",
      });
      facts.push({
        label: "승인 ① 상태",
        value: run.approvals.includes("plan")
          ? `승인 완료${planApproval?.approver ? ` (${planApproval.approver})` : ""}`
          : "미승인",
        tone: run.approvals.includes("plan") ? "good" : "warn",
      });
    } else if (s.stage === "provisioning") {
      facts.push({ label: "생성 VPC", value: target.vpcId ?? "미제공" });
      facts.push({ label: "Cloud SQL 인스턴스", value: target.dbInstanceId ?? "미제공" });
      facts.push({ label: "사설 IP", value: target.cloudSqlIp ?? "미제공" });
      facts.push({ label: "데이터베이스명", value: target.dbName ?? "미제공" });
    } else if (s.stage === "db_migration") {
      facts.push({
        label: "복사 진행률",
        value: cdc.copyPercent !== null ? `${cdc.copyPercent}%` : "미제공",
        tone: cdc.copyPercent === 100 ? "good" : "neutral",
      });
      facts.push({
        label: "복제 지연 (Lag)",
        value: cdc.lagFormatted ?? "미제공",
        tone: cdc.lagBytes === 0 ? "good" : "neutral",
      });
      facts.push({
        label: "테이블 대조",
        value:
          cdc.tables.length > 0
            ? `${cdc.tables.filter((t) => t.match).length}/${cdc.tables.length} 일치`
            : "미제공",
        tone: cdc.tables.length > 0 && cdc.tables.every((t) => t.match) ? "good" : "neutral",
      });
    } else if (s.stage === "app_deploy") {
      facts.push({ label: "Cloud Run URL", value: target.cloudRunUrl ?? "미제공" });
      facts.push({
        label: "헬스 체크",
        value: target.healthy === null ? "미제공" : target.healthy ? "정상" : "실패",
        tone: target.healthy === null ? "neutral" : target.healthy ? "good" : "bad",
      });
      facts.push({ label: "목적지 리전", value: target.providerSummary });
    } else if (s.stage === "cutover") {
      facts.push({
        label: "현재 트래픽 처리",
        value: serving ? SERVING_LABEL[serving] : "미제공",
        tone: serving === "target" ? "good" : "neutral",
      });
      facts.push({
        label: "전환 소요 (T1−T0)",
        value: cutoverSec !== null ? `${cutoverSec.toFixed(1)}초` : "미제공",
        tone: cutoverSec !== null ? "good" : "neutral",
      });
      facts.push({
        label: "읽기 실패 (프로버)",
        value: failedReqs !== null ? `${failedReqs}건` : "미제공",
        tone: failedReqs === 0 ? "good" : failedReqs !== null ? "bad" : "neutral",
      });
      facts.push({
        label: "순서 검증",
        value: sequence ? sequence.status : steps ? `${steps.length}단계 기록됨` : "미제공",
        tone: sequence?.status === "VERIFIED" ? "good" : "neutral",
      });
      if (lastCutoverApproval) {
        facts.push({
          label: "승인 ② 기록",
          value: `${lastCutoverApproval.approver ?? "이름 미기재"} (${formatTime(lastCutoverApproval.at)})`,
          tone: "good",
        });
      } else if (cutoverSecSrc) {
        facts.push({
          label: "측정 출처",
          value: cutoverSecSrc === "worker" ? "Worker 실측" : "에이전트 보고",
        });
      }
    }

    return {
      index: idx + 1,
      stage: s.stage,
      label: STAGE_LABEL[s.stage],
      state: s.state,
      progress: s.state === "succeeded" ? 100 : s.progress,
      manual: s.manual,
      message: s.message,
      updatedAt: s.updated_at ? formatTime(s.updated_at) : null,
      facts,
    };
  });
}

export function getOverallSummaryMetrics(run: RunStatus) {
  return {
    progress: overallProgress(run),
    serving: servingSide(run),
    rows: tableRowTotals(run),
    copyPercent: numberMetric(run, METRIC_KEYS.copyPercent),
    lagBytes: numberMetric(run, METRIC_KEYS.replicationLagBytes),
    mismatch: numberMetric(run, METRIC_KEYS.mismatchTables),
    failedRequests: numberMetric(run, METRIC_KEYS.failedRequests),
    cutoverSeconds: numberMetric(run, METRIC_KEYS.cutoverSeconds),
  };
}
