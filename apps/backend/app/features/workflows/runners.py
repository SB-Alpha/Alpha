"""단계별 모듈 연결. 각 runner 는 run 상태에서 그 모듈의 Input 을 만들어 모듈 `run(inp, ctx)` 을 부릅니다.

모듈은 자기 `schemas.py` 와 진입 함수 `service.run` 만 씁니다 (확정서 2절).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .engine import StageRunner
from .schemas import RunContext, RunStatus, StageName


class MissingStageOutput(RuntimeError):
    """요청이나 앞 단계 출력이 없어 이 단계 입력을 만들 수 없을 때."""


DISCOVERY_MERGED_MESSAGE = "assessment 에 통합 — 원본 조회는 planning 단계에서 합니다"


def _extract_plan_target_infra(run: RunStatus) -> dict[str, Any]:
    planning_out = run.outputs.get(StageName.PLANNING.value)
    if not planning_out:
        raise MissingStageOutput("planning 단계 출력이 없습니다")
    plan = planning_out.get("plan")
    if not plan:
        raise MissingStageOutput("planning 단계의 plan 출력이 없습니다")
    target_infra = (
        plan.get("target_infra")
        if isinstance(plan, dict)
        else getattr(plan, "target_infra", None)
    )
    if not target_infra:
        raise MissingStageOutput("plan 에 target_infra 가 없습니다")
    if hasattr(target_infra, "model_dump"):
        target_infra = target_infra.model_dump()
    return dict(target_infra)


def run_discovery(run: RunStatus, ctx: RunContext) -> dict[str, Any]:
    # 11/4 합의로 discovery·planning 은 assessment 하나로 합쳤습니다 (PR #27). 원본 조회 결과는
    # planning 출력의 `source` 에 있으므로 이 단계는 조회 없이 그 사실만 남기고 넘깁니다.
    ctx.report("running", 100, None, DISCOVERY_MERGED_MESSAGE)
    return {"merged_into": StageName.PLANNING.value, "note": DISCOVERY_MERGED_MESSAGE}


def run_assessment(run: RunStatus, ctx: RunContext) -> Any:
    # assessment 가 discovery·planning 을 합친 모듈입니다 (PR #27). 출력은 {source, plan, gaps}.
    req = run.request
    if not req.aws_credentials_ref:
        raise MissingStageOutput("원본 AWS 키 이름표가 없습니다")

    try:
        from app.features.assessment import service as assessment_service
        from app.features.assessment.schemas import AssessmentInput
    except ImportError as exc:
        raise RuntimeError("assessment 모듈을 찾을 수 없습니다") from exc

    inp = AssessmentInput(
        run_id=run.run_id,
        github_repo_url=req.github_repo_url,
        aws_credentials_ref=req.aws_credentials_ref,
        db_admin_ref=req.db_admin_ref or req.db_credentials_ref,
        aws_region=req.aws_region,
        target_region=req.target_region,
        ecs_service=req.ecs_service,
        rds_instance_id=req.rds_instance_id,
    )
    return assessment_service.run(inp, ctx)


def run_provisioning(run: RunStatus, ctx: RunContext) -> Any:
    # planning 출력의 target_infra 로 목적지 인프라(VPC, Cloud SQL)를 프로비저닝합니다 (PR #40).
    target_infra = _extract_plan_target_infra(run)

    from app.features.provisioning import service as provisioning_service
    from app.features.provisioning.schemas import ProvisioningInput

    inp = ProvisioningInput(
        run_id=run.run_id,
        target_infra=target_infra,
    )
    return provisioning_service.run(inp, ctx)


def run_db_migration(run: RunStatus, ctx: RunContext) -> Any:
    # assessment(source)와 provisioning(target) 결과를 조립하여 CDC DB 마이그레이션을 실행합니다.
    planning_out = run.outputs.get(StageName.PLANNING.value)
    if not planning_out or not planning_out.get("source"):
        raise MissingStageOutput("planning 단계의 원본(source) 출력이 없습니다")
    source = planning_out["source"]
    if hasattr(source, "model_dump"):
        source = source.model_dump()

    provisioning_out = run.outputs.get(StageName.PROVISIONING.value)
    if not provisioning_out or not provisioning_out.get("target"):
        raise MissingStageOutput("provisioning 단계의 대상(target) 출력이 없습니다")
    target = provisioning_out["target"]
    if hasattr(target, "model_dump"):
        target = target.model_dump()

    from app.features.db_migration import service as db_migration_service

    inp = {
        "run_id": run.run_id,
        "source": dict(source),
        "target": dict(target),
    }
    return db_migration_service.run(inp, ctx)


def run_app_deploy(run: RunStatus, ctx: RunContext) -> Any:
    """DB 마이그레이션 스키마 준비 후 목적지 Cloud Run 앱을 배포하고 /health/ready 200 을 검증합니다.

    실행 조건 (환경변수):
      - WORKER_URL: Cloudflare Worker 주소
      - WORKER_TOKEN_FILE: Worker 인증 토큰 파일 경로
      - GCP_PROJECT_ID: 배포 대상 GCP 프로젝트 ID
    """
    target_infra = _extract_plan_target_infra(run)

    from app.features.provisioning import service as provisioning_service
    from app.features.provisioning.schemas import AppDeployInput

    inp = AppDeployInput(
        run_id=run.run_id,
        target_infra=target_infra,
    )
    return provisioning_service.deploy_app(inp, ctx)


def run_cutover(
    run: RunStatus,
    ctx: RunContext,
    *,
    poll_interval_s: float = 1.0,
    timeout_s: float = 60.0,
) -> Any:
    """승인 ② 완료 후 CDC 에이전트에 cutover 요청(hold -> switch -> release)을 전달하고,
    에이전트가 stabilizing(전환 완료) 또는 failed/waiting_approval(롤백)에 도달할 때까지 대기합니다.
    """
    from app.features.db_migration.client import AgentSettings, CdcAgentClient
    from app.features.db_migration.guide_workflow import to_job_id

    job_id = to_job_id(run.run_id)
    ctx.report("running", 10, None, "트래픽 전환(Cutover) 요청 중")

    try:
        settings = AgentSettings.from_env()
    except KeyError:
        raise RuntimeError("CDC_AGENT_URL 환경변수가 설정되지 않아 트래픽 전환을 호출할 수 없습니다") from None

    # 승인 ② 실제 승인자 이름 기록 (감사 추적용)
    approved_by = "workflows"
    if run.message and ":" in run.message:
        approved_by = run.message.split(":", 1)[1].strip() or "workflows"

    agent = CdcAgentClient(settings)
    agent.cutover(job_id, approved_by=approved_by)

    start_t = time.monotonic()
    while True:
        job = agent.get_job(job_id)
        state = job.get("state")
        phase = job.get("phase")
        metrics = job.get("metrics")
        msg = job.get("message")

        # 1. 성공 상태 (에이전트가 stabilizing 또는 succeeded 도달)
        if phase == "stabilizing" or state == "succeeded":
            message = f"트래픽 전환 완료 ({msg})" if msg else "트래픽 전환 완료 (안정화 관찰 중)"
            ctx.report("running", 100, metrics, message)
            return job

        # 2. 롤백 상태 (전환 실패 후 원본 복귀)
        if state == "waiting_approval" or phase == "cutover_ready":
            raise RuntimeError(f"컷오버 실패로 원본으로 롤백되었습니다: {msg or '상세 불명'}")

        # 3. 영구 실패 상태
        if state in ("failed", "aborted"):
            raise RuntimeError(f"컷오버 실패 ({state}): {msg or '상세 불명'}")

        # 4. 진행 중 보고 및 타임아웃 검사
        if time.monotonic() - start_t > timeout_s:
            raise TimeoutError(f"트래픽 전환 완료 대기 시간({timeout_s}초)을 초과했습니다")

        progress = job.get("progress", 75)
        ctx.report("running", progress, metrics, msg or "트래픽 전환 진행 중...")
        time.sleep(poll_interval_s)


RUNNERS: Mapping[StageName, StageRunner] = {
    StageName.DISCOVERY: run_discovery,
    StageName.PLANNING: run_assessment,
    StageName.PROVISIONING: run_provisioning,
    StageName.DB_MIGRATION: run_db_migration,
    StageName.APP_DEPLOY: run_app_deploy,
    StageName.CUTOVER: run_cutover,
}
