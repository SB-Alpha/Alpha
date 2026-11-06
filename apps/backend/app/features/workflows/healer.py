"""자가치유(Self-Healing) 진단: 실패한 단계의 오류를 분석해 원인과 복구 방안을 제시합니다.

- ALPHA_AI_ENABLED=1 + `pip install -e ".[ai]"` 일 때 Claude 를 호출하고, 실패하면 규칙 기반 진단으로 넘어갑니다.
- 어떤 진단이었는지는 diagnosed_by 로 구분합니다.
- 수동 입력 추천값은 AI 가 만들지 않습니다. 시뮬레이션 모드에서만, 규칙 기반으로 제공합니다.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from pydantic import BaseModel, Field

from .schemas import RunStatus, StageName, WorkflowState

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
TIMEOUT_SECONDS = 30.0


class AiDiagnosis(BaseModel):
    """Claude 구조화 출력 형식. 키가 고정되지 않은 dict 는 빈 객체로 변환되므로 넣지 않습니다."""

    category: str = Field(description="장애 분류 (environment_dependency / credentials / network / schema / unknown)")
    root_cause: str = Field(description="근본 원인 요약 (한국어)")
    action_plan: list[str] = Field(description="운영자 조치 단계 3개 (한국어)")


class SelfHealingDiagnosis(AiDiagnosis):
    suggested_manual_input: dict[str, Any] | None = Field(
        default=None, description="시뮬레이션 모드에서 수동 입력에 쓸 수 있는 값"
    )
    diagnosed_by: str = Field(default="rule", description="진단 주체 (ai | rule)")


SYSTEM_PROMPT = """너는 클라우드 마이그레이션 오케스트레이터의 자가치유(Self-Healing) 진단 에이전트다.
파이프라인 실행 중 특정 단계에서 발생한 오류 메시지와 단계 상태를 분석하여, 운영자가 즉시 장애를 해소할 수 있도록 진단 리포트를 작성하라.
- category: environment_dependency(로컬 도구/바이너리 누락), credentials(키/토큰 오류), network(VPN/연결 타임아웃), schema(DB 구조/제약 충돌), unknown 중 하나 선택
- root_cause: 장애의 직접적 원인을 명쾌하게 한국어 1~2문장으로 설명
- action_plan: 운영자가 수행해야 할 구체적 해결 단계 정확히 3개 (예: 1. CLI 설치, 2. 설정 수정, 3. 재시도)
"""


def diagnose_run(run: RunStatus) -> SelfHealingDiagnosis:
    failed_stage = next((s for s in run.stages if s.state == WorkflowState.FAILED), None)

    error_msg = (failed_stage.message if failed_stage and failed_stage.message else run.message) or "알 수 없는 오류"
    stage_name = failed_stage.stage if failed_stage else (run.current_stage or StageName.DISCOVERY)

    if os.environ.get("ALPHA_AI_ENABLED") == "1":
        try:
            diag = _ask_claude_healer(run, stage_name, error_msg)
            if diag:
                return diag
        except Exception as exc:
            log.warning("AI 자가치유 진단 실패, 규칙 기반 진단 사용 (%s)", type(exc).__name__)

    return _rule_based_diagnosis(stage_name, error_msg)


def _simulation_enabled() -> bool:
    from app.features.db_migration.client import AgentSettings

    try:
        return AgentSettings.from_env().is_simulation
    except KeyError:
        return False


def _simulation_fixture(stage: StageName) -> dict[str, Any] | None:
    """시뮬레이션 전용 값. 실제 인프라가 아니므로 시뮬레이션이 아니면 제공하지 않습니다.

    credentials_ref 는 scripts/register_secrets.py 의 기본 이름표입니다.
    """
    if stage not in (StageName.PROVISIONING, StageName.APP_DEPLOY) or not _simulation_enabled():
        return None
    target = {
        "project_id": "simulation-project",
        "region": "asia-northeast3",
        "network": {
            "vpc_name": "simulation-vpc",
            "subnet_cidrs": ["10.30.0.0/24"],
            "psa_range_cidr": "10.31.0.0/16",
        },
        "db": {
            "instance": "simulation-cloudsql",
            "private_ip": "10.31.0.5",
            "port": 5432,
            "database": "app_db",
            "version": "POSTGRES_16",
            "ssl_mode": "ENCRYPTED_ONLY",
        },
        "app": {"url": None, "db_user": "app_user"},
        "credentials_ref": {
            "cloud": "secret://default/target-cloud",
            "admin": "secret://default/target-admin",
        },
    }
    common = {"simulated": True, "target": target, "changes": {}, "applied": False}
    if stage == StageName.PROVISIONING:
        return {**common, "problems": []}
    return {**common, "app_url": None, "healthy": False}


def _rule_based_diagnosis(stage: StageName, error_msg: str) -> SelfHealingDiagnosis:
    lowered = error_msg.lower()

    if "terraform" in lowered and "찾을 수 없습니다" in lowered:
        return SelfHealingDiagnosis(
            category="environment_dependency",
            root_cause="호스트 시스템에 terraform CLI 실행 바이너리가 설치되어 있지 않아 자동 인프라 생성이 중단되었습니다.",
            action_plan=[
                "운영체제 패키지 관리자(예: brew install terraform)로 terraform CLI를 설치합니다.",
                "설치 후 [재시도]로 실패한 단계를 다시 실행합니다.",
                "이미 만들어 둔 인프라가 있으면 [수동 입력]에 그 실제 값을 넣어 진행합니다.",
            ],
            suggested_manual_input=_simulation_fixture(stage),
            diagnosed_by="rule",
        )

    if "cdc_agent_url" in lowered:
        return SelfHealingDiagnosis(
            category="network",
            root_cause="CDC_AGENT_URL 환경변수가 지정되지 않았거나 OCI CDC 제어 노드에 접근할 수 없습니다.",
            action_plan=[
                "환경변수 .env에 올바른 CDC_AGENT_URL 주소를 등록하고 백엔드를 재시작합니다.",
                "OCI와의 IPsec VPN 터널 및 방화벽 헬스체크를 점검합니다.",
                "시연 환경이면 CDC_AGENT_URL을 비우고 ALPHA_CDC_SIMULATION=1로 시뮬레이션 모드를 켭니다. 화면에 시뮬레이션으로 표시됩니다.",
            ],
            diagnosed_by="rule",
        )

    if "invalidclienttokenid" in lowered or "credentials" in lowered or "인증" in lowered:
        return SelfHealingDiagnosis(
            category="credentials",
            root_cause="원본 클라우드(AWS) API 호출 자격 증명이 유효하지 않거나 토큰이 만료되었습니다.",
            action_plan=[
                "입력된 AWS Access Key ID 및 Secret Access Key의 유효성을 점검합니다.",
                "IAM 사용자에게 SecurityAudit 및 ReadOnlyAccess 권한이 연결되어 있는지 확인합니다.",
                "새 자격증명을 암호화 시크릿 저장소에 갱신 등록한 후 재시도합니다.",
            ],
            diagnosed_by="rule",
        )

    return SelfHealingDiagnosis(
        category="unknown",
        root_cause=f"'{stage.value}' 단계 실행 중 예외가 발생했습니다: {error_msg}",
        action_plan=[
            "백엔드 상세 로그(uvicorn console)에서 예외 스택 트레이스를 확인합니다.",
            "해당 단계의 입력값 및 이전 단계 출력 데이터 정합성을 점검합니다.",
            "필요 시 수동 입력에 실제 값을 넣어 진행합니다.",
        ],
        diagnosed_by="rule",
    )


def _ask_claude_healer(run: RunStatus, stage: StageName, error_msg: str) -> SelfHealingDiagnosis | None:
    import anthropic

    client = anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=1)

    from .runners import scrub_sensitive_text

    # 자격 증명 참조·호스트가 든 요청과 단계 출력은 외부 API 로 보내지 않습니다.
    # 에러 및 단계 메시지의 URL, 패스워드, 호스트 등 민감 정보도 살균(scrub) 후 전송합니다.
    payload = {
        "failed_stage": stage.value,
        "error_message": scrub_sensitive_text(error_msg, fallback="오류 발생"),
        "stages": [
            {
                "stage": s.stage.value,
                "state": s.state.value,
                "message": scrub_sensitive_text(s.message) if s.message else None,
            }
            for s in run.stages
        ],
    }

    response = client.messages.parse(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        output_format=AiDiagnosis,
    )
    parsed = response.parsed_output
    if parsed is None:
        return None
    return SelfHealingDiagnosis(**parsed.model_dump(), diagnosed_by="ai")
