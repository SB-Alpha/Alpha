"""원본 AWS 세션 만들기. db.py · app.py 가 같이 씁니다.

AWS 키 이름표 형식:
- `secret://...` : backend DB 에 저장된 키. `app.core.secrets.resolve()` 로 꺼냅니다 (cdc 명세 8절).
  저장된 값은 `{"aws_access_key_id", "aws_secret_access_key", "aws_session_token"(선택)}`
  (db_migration #31 `source-cloud` 와 같은 이름). 예전 이름 `access_key_id` 등도 받습니다.
- `profile:<이름>` : 로컬 개발용. 내 PC 의 AWS CLI 프로필을 씁니다. 서버 프로필을 화면 입력으로
  고르지 못하게, 환경변수 `ALPHA_ALLOW_AWS_PROFILE=1` 일 때만 받습니다 (cli.py 가 켬).

키 원문은 로그·gaps·결과 어디에도 넣지 않습니다.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol


class AwsSession(Protocol):
    """boto3.Session 과 같은 모양. 테스트에서는 가짜 세션을 넣습니다."""

    def client(self, service_name: str, region_name: str | None = None) -> Any: ...


def error_code(e: Exception) -> str:
    """AWS 오류에서 코드만 꺼냅니다. 메시지는 길고 계정 정보가 섞일 수 있어 남기지 않습니다."""
    response = getattr(e, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code:
            return str(code)
    return type(e).__name__


ALLOW_PROFILE_ENV = "ALPHA_ALLOW_AWS_PROFILE"


def _pick(creds: dict, name: str, required: bool = True):
    """`aws_<name>`(#31 source-cloud) 를 먼저, 없으면 예전 이름 `<name>`. 필수인데 없으면 KeyError."""
    for key in (f"aws_{name}", name):
        if creds.get(key):
            return creds[key]
    if required:
        raise KeyError(name)
    return None


def session_for(ref: str, region: str) -> tuple[AwsSession | None, str | None]:
    """(세션, 실패 이유). 실패하면 세션은 None 이고 이유는 gaps 에 넣을 문장입니다."""
    try:
        import boto3
    except ImportError:
        return None, "boto3 미설치로 AWS 조회 불가"

    if ref.startswith("profile:"):
        if os.environ.get(ALLOW_PROFILE_ENV) != "1":
            return None, "profile: 이름표는 로컬 CLI 에서만 씁니다 (secret:// 로 넘겨 주세요)"
        try:
            return boto3.Session(profile_name=ref.removeprefix("profile:"), region_name=region), None
        except Exception as e:  # 프로필이 없으면 ProfileNotFound
            return None, f"AWS 프로필을 열 수 없음 ({type(e).__name__})"

    if ref.startswith("secret://"):
        try:
            from app.core.secrets import resolve  # 안재현님 제안 (cdc 명세 8절). 아직 없으면 ImportError
        except ImportError:
            return None, "AWS 키 이름표를 풀 수 없음 (core/secrets 미구현)"
        try:
            creds = resolve(ref)  # PR #29: dict 를 돌려줌
            if isinstance(creds, str):
                creds = json.loads(creds)
            return boto3.Session(
                aws_access_key_id=_pick(creds, "access_key_id"),
                aws_secret_access_key=_pick(creds, "secret_access_key"),
                aws_session_token=_pick(creds, "session_token", required=False),
                region_name=region,
            ), None
        except Exception as e:
            return None, f"AWS 키 이름표 해석 실패 ({type(e).__name__})"

    return None, "지원하지 않는 AWS 키 이름표 형식 (secret:// 또는 profile:)"
