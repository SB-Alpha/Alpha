"""AI 자가치유(Self-Healing) 진단 엔진: 파이프라인 장애 발생 시 원인을 스스로 진단하고 복구 방안을 제시합니다.

- 본선 심사 10점(AI 활용도) 및 엔터프라이즈 자가치유 전략 대응
- 실패한 단계의 오류 메시지 및 실행 컨텍스트를 분석하여 근본 원인과 조치 방안을 도출합니다.
- 환경 변수 ALPHA_AI_ENABLED=1 일 때 Claude 를 호출하며, 실패 시 규칙 기반 진단으로 안전하게 폴백합니다.
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


class SelfHealingDiagnosis(BaseModel):
    category: str = Field(description="장애 분류 (environment_dependency / credentials / network / schema / unknown)")
    root_cause: str = Field(description="근본 원인 요약 (한국어)")
    action_plan: list[str] = Field(description="운영자 조치 단계 3개 (한국어)")
    suggested_manual_input: dict[str, Any] | None = Field(
        default=None, description="수동 주입 폼(POST /runs/{id}/stages/{stage}/manual)에 채울 수 있는 권장 JSON"
    )
    diagnosed_by: str = Field(default="rule", description="진단 주체 (ai | rule)")


SYSTEM_PROMPT = """너는 클라우드 마이그레이션 오케스트레이터의 자가치유(Self-Healing) 진단 에이전트다.
파이프라인 실행 중 특정 단계에서 발생한 오류 메시지와 실행 정보를 분석하여, 운영자가 즉시 장애를 해소할 수 있도록 진단 리포트를 작성하라.
- category: environment_dependency(로컬 도구/바이너리 누락), credentials(키/토큰 오류), network(VPN/연결 타임아웃), schema(DB 구조/제약 충돌), unknown 중 하나 선택
- root_cause: 장애의 직접적 원인을 명쾌하게 한국어 1~2문장으로 설명
- action_plan: 운영자가 수행해야 할 구체적 해결 단계 정확히 3개 (예: 1. CLI 설치, 2. 수동 주입 기능 활용, 3. 재시도)
- suggested_manual_input: 해당 단계가 수동 입력(POST /stages/{stage}/manual)을 지원하는 경우, 다음 단계로 통과시킬 수 있는 최소 유효 JSON 딕셔너리를 제안 (없으면 null)
"""


def diagnose_run(run: RunStatus) -> SelfHealingDiagnosis:
    failed_stage = None
    for stage in run.stages:
        if stage.state == WorkflowState.FAILED or (stage.message and "실패" in stage.message):
            failed_stage = stage
            break

    error_msg = (failed_stage.message if failed_stage and failed_stage.message else run.message) or "알 수 없는 오류"
    stage_name = failed_stage.stage if failed_stage else (run.current_stage or StageName.DISCOVERY)

    # 1. AI 호출 시도 (ALPHA_AI_ENABLED=1 일 때)
    if os.environ.get("ALPHA_AI_ENABLED") == "1":
        try:
            diag = _ask_claude_healer(run, stage_name, error_msg)
            if diag:
                return diag
        except Exception as exc:
            log.warning("AI 자가치유 진단 실패, 규칙 기반 진단 사용 (%s)", type(exc).__name__)

    # 2. 규칙 기반 폴백 진단
    return _rule_based_diagnosis(stage_name, error_msg)


def _rule_based_diagnosis(stage: StageName, error_msg: str) -> SelfHealingDiagnosis:
    lowered = error_msg.lower()

    if "terraform" in lowered and "찾을 수 없습니다" in lowered:
        return SelfHealingDiagnosis(
            category="environment_dependency",
            root_cause="호스트 시스템에 terraform CLI 실행 바이너리가 설치되어 있지 않아 자동 인프라 생성이 중단되었습니다.",
            action_plan=[
                "운영체제 패키지 관리자(예: brew install terraform)로 terraform CLI를 설치합니다.",
                "또는 콘솔의 [수동 입력] 탭을 열어 생성할 대상 인프라(VPC, Cloud SQL 정보)를 수동 주입합니다.",
                "수동 주입 후 워크플로우를 재개하면 다음 단계(DB 마이그레이션)로 즉시 전이됩니다.",
            ],
            suggested_manual_input={
                "target": {
                    "project_id": "alpha-target-513617",
                    "region": "asia-northeast3",
                    "cloud_sql": {
                        "instance_id": "alpha-cloudsql-sim",
                        "private_ip": "10.0.1.5",
                        "database": "app_db",
                        "admin_username": "postgres",
                    },
                    "app_service": {
                        "service_name": "target-app",
                        "service_url": "https://target-app-sim.a.run.app",
                    },
                }
            },
            diagnosed_by="rule",
        )

    if "cdc_agent_url" in lowered:
        return SelfHealingDiagnosis(
            category="network",
            root_cause="CDC_AGENT_URL 환경변수가 지정되지 않았거나 OCI CDC 제어 노드에 접근할 수 없습니다.",
            action_plan=[
                "환경변수 .env에 올바른 CDC_AGENT_URL 주소를 등록하고 백엔드를 재시작합니다.",
                "OCI와의 IPsec VPN 터널 및 방화벽 헬스체크를 점검합니다.",
                "현장 시연 환경인 경우 ALPHA_CDC_SIMULATION=1 플래그를 활성화하여 모의 복제 모드로 전환합니다.",
            ],
            suggested_manual_input=None,
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
            suggested_manual_input=None,
            diagnosed_by="rule",
        )

    return SelfHealingDiagnosis(
        category="unknown",
        root_cause=f"'{stage.value}' 단계 실행 중 예외가 발생했습니다: {error_msg}",
        action_plan=[
            "백엔드 상세 로그(uvicorn console)에서 예외 스택 트레이스를 확인합니다.",
            "해당 단계의 입력값 및 이전 단계 출력 데이터 정합성을 점검합니다.",
            "필요 시 수동 입력 인터페이스를 통해 정상 픽스처를 주입하고 진행합니다.",
        ],
        suggested_manual_input=None,
        diagnosed_by="rule",
    )


def _ask_claude_healer(run: RunStatus, stage: StageName, error_msg: str) -> SelfHealingDiagnosis | None:
    import anthropic

    client = anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=1)

    payload = {
        "run_id": run.run_id,
        "failed_stage": stage.value,
        "error_message": error_msg,
        "request": run.request.model_dump(mode="json"),
        "stage_outputs": {k: v for k, v in run.outputs.items() if k != "planning"},
    }

    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            output_format=SelfHealingDiagnosis,
        )
        diag = response.parsed_output
        if diag:
            diag.diagnosed_by = "ai"
            return diag
    except Exception as exc:
        log.warning("Claude 자가치유 호출 오류: %s", exc)
        return None
