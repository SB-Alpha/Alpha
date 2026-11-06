"""CSP 중립 SSH 포트포워딩. 환경변수: ALPHA_BASTION_SSH_USER(기본 alpha), ALPHA_BASTION_SSH_KEY, ALPHA_BASTION_PROXY_COMMAND(선택, %h/%p),
ALPHA_BASTION_KNOWN_HOSTS(known_hosts 파일, 운영 시 설정 권장: 없으면 accept-new 로 첫 접속 키를 신뢰)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from typing import Iterator

from app.features.assessment.bastion import (
    BASTION_TAG_KEY,
    BASTION_TAG_VALUE,
    _HOST_RE,
    _free_port,
    _last_line,
    _listening,
    _posix,
    _stop,
)

BASTION_TAG = (BASTION_TAG_KEY, BASTION_TAG_VALUE)


class SshTunnelError(Exception):
    """ssh 터널 실패. 메시지에 키 경로·비밀값을 넣지 않는다."""


def ssh_user() -> str:
    return os.environ.get("ALPHA_BASTION_SSH_USER") or "alpha"


def ssh_key_path() -> str | None:
    return os.environ.get("ALPHA_BASTION_SSH_KEY") or None


@contextmanager
def forward(
    bastion_host: str,
    target_host: str,
    target_port: int,
    *,
    user: str | None = None,
    key_path: str | None = None,
    proxy_command: str | None = None,
    ssh_port: int = 22,
    ready_timeout: float = 30.0,
) -> Iterator[tuple[str, int]]:
    """ssh -L 로 ("127.0.0.1", 로컬포트)를 열고, 나갈 때 반드시 닫는다. 인자 None 이면 환경변수 값 사용."""
    user = user or ssh_user()
    key_path = key_path or ssh_key_path()
    if proxy_command is None:
        proxy_command = os.environ.get("ALPHA_BASTION_PROXY_COMMAND") or None
    for h in (bastion_host, target_host):
        if not _HOST_RE.match(h or ""):
            raise SshTunnelError("호스트 형식이 올바르지 않음")
    if not (isinstance(target_port, int) and 0 < target_port < 65536):
        raise SshTunnelError("대상 포트 범위가 올바르지 않음")
    if not (isinstance(ssh_port, int) and 0 < ssh_port < 65536):
        raise SshTunnelError("ssh 포트 범위가 올바르지 않음")
    if not _HOST_RE.match(user or ""):
        raise SshTunnelError("ssh 사용자 형식이 올바르지 않음")
    if not shutil.which("ssh"):
        raise SshTunnelError("ssh 바이너리 없음: OpenSSH 클라이언트를 설치하세요")

    port = _free_port()
    cmd = [
        "ssh", "-N", "-L", f"127.0.0.1:{port}:{target_host}:{target_port}",
        "-p", str(ssh_port),
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
    ]
    kh = os.environ.get("ALPHA_BASTION_KNOWN_HOSTS")
    cmd += ["-o", f"UserKnownHostsFile={kh}", "-o", "StrictHostKeyChecking=yes"] if kh else ["-o", "StrictHostKeyChecking=accept-new"]
    if key_path:
        cmd += ["-i", key_path, "-o", "IdentitiesOnly=yes"]
    if proxy_command:
        cmd += ["-o", f"ProxyCommand={proxy_command}"]
    cmd.append(f"{user}@{bastion_host}")
    proc = subprocess.Popen(
        cmd, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **({"start_new_session": True} if _posix() else {}),
    )
    try:
        deadline = time.monotonic() + ready_timeout
        while not _listening(port):
            if proc.poll() is not None:
                why = _last_line(proc)
                raise SshTunnelError("ssh 터널 프로세스가 먼저 종료됨" + (f": {why}" if why else ""))
            if time.monotonic() >= deadline:
                raise SshTunnelError(f"ssh 터널 준비 시간 초과 ({ready_timeout:g}초)")
            time.sleep(0.1)
        yield "127.0.0.1", port
    finally:
        _stop(proc)
