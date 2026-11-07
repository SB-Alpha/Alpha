"""assessment 진입 함수. workflows 가 run() 을 호출합니다. (결과 조립: 이주호)

흐름: source_kind 로 원본 읽기 → 합쳐서 plan.py 판정 → AssessmentOutput (어느 쪽이든 같은 형식)
- aws(기본): AWS 세션 → db.py(김준석) · app.py(이주호) 동시 조회
- manual: manual.py 가 사람이 입력한 값으로 같은 형식을 만듦 (온프레미스 등 API 가 없는 원본용)
- gcp / azure: cloud_source.py 가 Cloud Run·Cloud SQL / Container Apps·PostgreSQL Flexible 을 REST 로 조회
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from . import ai as ai_part
from . import ai_mapping as ai_mapping_part
from . import cloud_source as cloud_part
from . import app as app_part
from . import db as db_part
from . import manual as manual_part
from . import plan as plan_part
from .aws import AwsSession, session_for
from .schemas import (
    AppAssessment,
    AppInfo,
    AssessmentInput,
    AssessmentOutput,
    DbAssessment,
    InventoryForPlanning,
    PlanningInput,
    SourceApp,
    SourceCredentials,
    SourceInfo,
    TargetSelection,
)


class Reporter(Protocol):
    """workflows RunContext 와 같은 시그니처 (PR #22). 확정되면 그 타입으로 교체."""

    def report(self, state: str, progress: int, metrics: dict[str, Any] | None = None,
               message: str | None = None) -> None: ...


def run(inp: AssessmentInput, ctx: Reporter | None = None,
        session: AwsSession | None = None) -> AssessmentOutput:
    def report(progress: int, message: str, state: str = "running") -> None:
        if ctx is not None:
            ctx.report(state, progress, None, message)

    gaps: list[str] = []

    if inp.source_kind == "manual":
        report(20, "직접 입력한 원본 정보 정리")
        db_res, app_res = manual_part.collect(inp)
        region = inp.manual.region if inp.manual else None
    elif inp.source_kind in ("gcp", "azure"):
        db_res, app_res, region = _collect_cloud(inp, report, gaps)
    else:
        region = inp.aws_region
        db_res, app_res = _collect_aws(inp, session, report, gaps)
    gaps += db_res.gaps + app_res.gaps

    report(80, "필수 조건 판정·목적지 계획")
    db_facts = db_res.facts
    if db_facts.username is None and app_res.db_user:
        # 목적지 DB 사용자는 앱이 실제로 쓰는 PGUSER 를 따릅니다
        db_facts = db_facts.model_copy(update={"username": app_res.db_user})
    inventory = InventoryForPlanning(app=app_res.facts, db=db_facts, gaps=gaps)
    plan = plan_part.build(PlanningInput(inventory=inventory, target=TargetSelection(region=inp.target_region)))
    if ai_part.enabled():
        report(90, "AI 로 요약·위험 설명 보강")
        plan = ai_part.enhance(plan)  # 실패하면 규칙 결과 그대로
        plan = ai_mapping_part.attach(plan, inventory)  # 환경 변수 대응·빠진 의존성 제안. 판정은 안 바꿈

    source = SourceInfo(
        account_id=app_res.account_id,
        region=region,
        db=db_res.source_db,
        network=db_res.network,
        app=SourceApp(db_user=app_res.db_user),
        tables=[f"{t.schema_name}.{t.name}" for t in db_facts.tables],
        credentials_ref=SourceCredentials(cloud=_cloud_ref(inp, gaps),
                                          db_admin=_ref_or_none(inp.db_admin_ref, "db_admin_ref", gaps)),
    )

    # 단계 성공은 run() 반환으로 workflows 가 정합니다. report 는 진행 중 상태만 (PR #22 RunContext)
    report(100, "계획 생성 완료")
    return AssessmentOutput(source=source, plan=plan, gaps=gaps)


def _collect_aws(inp: AssessmentInput, session: AwsSession | None, report, gaps: list[str]):
    """AWS 계정 조회 (source_kind="aws"). 실패 이유는 gaps 에 넣고, 조회 결과 두 개를 돌려줍니다."""
    report(5, "원본 AWS 연결")
    if session is None:
        session, reason = session_for(inp.aws_credentials_ref, inp.aws_region)
        if reason:
            gaps.append(reason)

    if session is None:
        db_res = DbAssessment()
        app_res = AppAssessment(facts=AppInfo(service_name=app_part.fallback_name(inp)))
    else:
        # DB 쪽(RDS → bastion → SSM 터널 → PG)과 앱 쪽(ECS·ECR)은 서로 기다릴 게 없어 동시에 조회합니다.
        # 진행 보고(ctx.report)는 이 스레드에서만 합니다.
        report(20, "DB·네트워크·앱 동시 조회")
        db_gaps: list[str] = []
        app_gaps: list[str] = []
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="assessment") as pool:
            db_job = pool.submit(_safe, "DB·네트워크 조회", lambda: db_part.collect(inp, session),
                                 DbAssessment(), db_gaps)
            app_job = pool.submit(_safe, "앱 조회", lambda: app_part.collect(inp, session),
                                  AppAssessment(facts=AppInfo(service_name=app_part.fallback_name(inp))), app_gaps)
            app_res = app_job.result()
            if not db_job.done():
                report(50, "앱 조회 완료, DB·네트워크 조회 중")
            db_res = db_job.result()
        gaps += db_gaps + app_gaps  # 순서는 예전(DB → 앱)과 같게
    return db_res, app_res


def _collect_cloud(inp: AssessmentInput, report, gaps: list[str]):
    """GCP·Azure 조회. 인증이 실패해도 예외 대신 gaps 로 남겨 승인 불가가 되게 합니다."""
    src = inp.cloud_source
    name = src.app_name if src else app_part.fallback_name(inp)
    label = "GCP" if inp.source_kind == "gcp" else "Azure"
    report(20, f"원본 {label} 앱·DB 조회")
    collect = cloud_part.collect_gcp if inp.source_kind == "gcp" else cloud_part.collect_azure
    try:
        return collect(inp)
    except Exception as e:  # 토큰 발급 실패 등. 메시지에 값이 섞일 수 있어 클래스명만
        gaps.append(f"원본 {label} 인증·조회 실패 ({type(e).__name__})")
        return DbAssessment(), AppAssessment(facts=AppInfo(service_name=name)), src.region if src else None


def _cloud_ref(inp: AssessmentInput, gaps: list[str]) -> str | None:
    if inp.source_kind in ("gcp", "azure"):
        ref = inp.cloud_source.credentials_ref if inp.cloud_source else None
        return _ref_or_none(ref, "cloud_source.credentials_ref", gaps)
    return _ref_or_none(inp.aws_credentials_ref, "aws_credentials_ref", gaps)


# 결과로 내보내도 되는 이름표 모양. 이 밖의 문자열은 비밀 원문일 수 있어 그대로 돌려주지 않습니다
REF_PREFIXES = ("secret://", "env://", "profile:")


def _ref_or_none(ref: str | None, field: str, gaps: list[str]) -> str | None:
    if ref is None or ref.startswith(REF_PREFIXES):
        return ref
    gaps.append(f"{field} 가 이름표 형식(secret://…)이 아니라 결과에 남기지 않음")
    return None


def _safe(what: str, fn, fallback, gaps: list[str]):
    """한쪽 조회가 예외로 죽어도 다른 쪽 결과로 계획은 나오게 합니다. 실패는 gaps 에 남아 승인 불가가 됩니다."""
    try:
        return fn()
    except Exception as e:
        gaps.append(f"{what} 중 오류 ({type(e).__name__})")
        return fallback
