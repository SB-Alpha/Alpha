"""전환 중 사용자 쪽에서 본 서비스 상태를 실측하는 외부 프로버.

Worker 공개 주소로 GET 을 순서대로 보내 실패 수와 대기 시간을 잽니다. 요청을 한 번에 하나씩 보내므로
게이트가 요청을 붙잡으면 그 시간이 지연값에 그대로 드러납니다. 읽기 요청만 보내므로 쓰기 실패는 재지 않습니다.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from .schemas import RunContext

log = logging.getLogger(__name__)

PROBE_URL_ENV = "ALPHA_PROBE_URL"
DEFAULT_INTERVAL_S = 0.2
DEFAULT_TIMEOUT_S = 10.0
ORIGIN_HEADER = "X-Gate-Origin"
# urllib 기본값(Python-urllib/x.y)은 Cloudflare 봇 차단에 403 을 받아, 정상인 서비스가 실패로 집계됩니다.
USER_AGENT = "alpha-workflows/1.0"


def _percentile(values: list[float], percent: int) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)]


class TrafficProber:
    def __init__(
        self,
        url: str,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._url = url
        self._interval_s = interval_s
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="traffic-prober", daemon=True)
        self._latencies_ms: list[float] = []
        self._failed = 0
        self._last_origin: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        """요청을 멈추고 최종 측정값을 돌려줍니다."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(self._timeout_s + 1)
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        """지금까지의 측정값. 보낸 요청이 없으면 모르는 값이므로 비워 둡니다."""
        with self._lock:
            if not self._latencies_ms:
                return {}
            metrics: dict[str, Any] = {
                "failed_requests": self._failed,
                "probe_requests": len(self._latencies_ms),
                "probe_p99_ms": round(_percentile(self._latencies_ms, 99), 1),
                "probe_max_ms": round(max(self._latencies_ms), 1),
            }
            if self._last_origin:
                metrics["probe_last_origin"] = self._last_origin
            return metrics

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._probe_once()
            self._stop.wait(self._interval_s)

    def _probe_once(self) -> None:
        started = time.perf_counter()
        origin: str | None = None
        try:
            request = urllib.request.Request(self._url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                response.read()
                origin = response.headers.get(ORIGIN_HEADER)
            is_failure = False
        except urllib.error.HTTPError as exc:
            origin = exc.headers.get(ORIGIN_HEADER)
            is_failure = True
        except (OSError, ValueError):
            # 연결 실패·시간 초과. URLError 와 socket.timeout 은 OSError 입니다.
            is_failure = True
        elapsed_ms = (time.perf_counter() - started) * 1000
        with self._lock:
            self._latencies_ms.append(elapsed_ms)
            self._failed += int(is_failure)
            self._last_origin = origin or self._last_origin


class ProbedContext:
    """모듈 보고에 프로버 실측값을 더합니다. 같은 키는 실측값이 우선합니다. 프로버가 없으면 보고만 넘깁니다."""

    def __init__(self, ctx: RunContext, prober: TrafficProber | None) -> None:
        self._ctx = ctx
        self._prober = prober
        self._last_progress = 0

    def report(
        self,
        state: str,
        progress: int,
        metrics: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        self._last_progress = progress
        probed = self._prober.snapshot() if self._prober else {}
        self._ctx.report(state, progress, {**(metrics or {}), **probed}, message)

    def finish(self, extra: dict[str, Any] | None = None) -> None:
        """프로버를 멈추고 최종값을 남깁니다. 실패·롤백으로 끝나도 측정값이 남도록 finally 에서 부릅니다."""
        final = self._prober.stop() if self._prober else {}
        self._ctx.report("running", self._last_progress, {**final, **(extra or {})})


def prober_from_env() -> TrafficProber | None:
    """설정이 없거나 쓸 수 없으면 프로버를 만들지 않습니다. 이때 화면은 '미제공' 입니다."""
    url = os.environ.get(PROBE_URL_ENV, "").strip()
    if not url:
        return None
    try:
        parts = urlsplit(url)
        is_usable = parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.username
    except ValueError:
        is_usable = False
    if not is_usable:
        # 주소에 비밀값이 섞여 있을 수 있어 값은 로그에 남기지 않습니다.
        log.warning("%s 값을 쓸 수 없어 프로버를 끕니다", PROBE_URL_ENV)
        return None
    return TrafficProber(url)
