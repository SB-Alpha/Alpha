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
import threading
from typing import Any, Protocol

# AWS 호출 하나가 오래 걸려도 단계 전체가 멈추지 않게 (botocore 기본값: 연결·읽기 60초, 재시도 legacy)
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 20
TOTAL_ATTEMPTS = 3  # 첫 시도 포함


class AwsSession(Protocol):
    """boto3.Session 과 같은 모양. 테스트에서는 가짜 세션을 넣습니다."""

    def client(self, service_name: str, region_name: str | None = None) -> Any: ...


class CachedSession:
    """boto3.Session 을 감쌉니다. db.py · app.py 가 동시에 써도 안전하게.

    - boto3 Session.client() 는 스레드 안전하지 않고 만들 때마다 서비스 모델을 읽어 느립니다.
      그래서 (서비스, 리전)마다 한 번만, 잠근 채로 만듭니다. 만들어진 클라이언트는 스레드 안전합니다.
    - 모든 클라이언트에 짧은 시간 제한과 표준 재시도를 겁니다.
    """

    def __init__(self, raw: Any, config: Any = None):
        self.raw = raw
        self._config = config
        self._lock = threading.Lock()
        self._clients: dict[tuple[str, str | None], Any] = {}

    def client(self, service_name: str, region_name: str | None = None) -> Any:
        key = (service_name, region_name)
        with self._lock:
            if key not in self._clients:
                kwargs: dict[str, Any] = {"region_name": region_name}
                if self._config is not None:
                    kwargs["config"] = self._config
                self._clients[key] = self.raw.client(service_name, **kwargs)
            return self._clients[key]

    def get_credentials(self) -> Any:  # bastion 이 SSM 터널 자식 프로세스에 넘길 때 씀
        get = getattr(self.raw, "get_credentials", None)
        return get() if get else None


def _client_config() -> Any:
    try:
        from botocore.config import Config
    except ImportError:
        return None
    return Config(connect_timeout=CONNECT_TIMEOUT_S, read_timeout=READ_TIMEOUT_S,
                  retries={"total_max_attempts": TOTAL_ATTEMPTS, "mode": "standard"})


def _wrap(raw: Any) -> CachedSession:
    return CachedSession(raw, _client_config())


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
            return _wrap(boto3.Session(profile_name=ref.removeprefix("profile:"), region_name=region)), None
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
            return _wrap(boto3.Session(
                aws_access_key_id=_pick(creds, "access_key_id"),
                aws_secret_access_key=_pick(creds, "secret_access_key"),
                aws_session_token=_pick(creds, "session_token", required=False),
                region_name=region,
            )), None
        except Exception as e:
            return None, f"AWS 키 이름표 해석 실패 ({type(e).__name__})"

    return None, "지원하지 않는 AWS 키 이름표 형식 (secret:// 또는 profile:)"
