# Cloudflare Worker(트래픽 게이트)의 target 제어 명령을 부르는 클라이언트.
# app_deploy 로 새 Cloud Run 주소가 생기면, 전환(switch) 전에 Worker 가 그 주소로 보내도록 알린다.
# 이 명령은 WORKER_API.md 규격 밖의 보조 명령이다 (infra/cloudflare/worker/src/index.js).
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .terraform import ProvisioningError

TIMEOUT_S = 10
# 파이썬 기본 User-Agent(Python-urllib)는 Cloudflare 가 봇으로 보고 막는다 (HTTP 403, error code 1010).
USER_AGENT = "alpha-provisioning/1.0"


class WorkerClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token  # 오류 메시지나 로그에 싣지 않는다

    @classmethod
    def from_env(cls) -> WorkerClient | None:
        # WORKER_URL 이 없으면 None. 토큰은 WORKER_TOKEN_FILE(파일 경로)을 먼저 보고, 없으면 WORKER_TOKEN 을 본다.
        base_url = os.getenv("WORKER_URL")
        if not base_url:
            return None
        token_file = os.getenv("WORKER_TOKEN_FILE")
        token = Path(token_file).read_text().strip() if token_file else os.getenv("WORKER_TOKEN", "")
        if not token:
            raise ProvisioningError("WORKER_URL 은 있는데 토큰이 없습니다. WORKER_TOKEN_FILE 이나 WORKER_TOKEN 을 지정하세요")
        return cls(base_url, token)

    def _send(self, method: str, body: dict[str, Any] | None = None, *, command: str = "target") -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self.base_url}/control/{command}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310  (운영자가 정한 Worker 주소)
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read()).get("error", "")
            except (ValueError, AttributeError):
                pass
            raise ProvisioningError(f"Worker {command} 명령 실패 (HTTP {exc.code}) {detail}".strip()) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise ProvisioningError(f"Worker 에 연결하지 못했습니다: {type(exc).__name__}") from None

    def status(self) -> dict[str, Any]:
        return self._send("GET", command="status")

    def get_target(self) -> str | None:
        return self._send("GET").get("url")

    def set_target(self, url: str) -> None:
        # 설정한 뒤 다시 읽어서 확인한다. Worker 는 끝의 / 를 떼어 저장하므로 비교할 때 맞춘다.
        self._send("POST", {"url": url})
        stored = self.get_target()
        if stored != url.rstrip("/"):
            raise ProvisioningError(f"Worker 에 저장된 주소가 다릅니다 (기대 {url}, 실제 {stored})")
