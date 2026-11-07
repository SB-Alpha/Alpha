"""SSM bastion 찾기와 포트포워딩 터널 열기.

bastion 은 이미 프로비저닝돼 있다고 보고 태그로 찾아 쓴다. 키 원문은 로그·예외에 남기지 않는다.
"""

from __future__ import annotations

import os
import signal
import re
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from app.features.assessment.aws import AwsSession, error_code

BASTION_TAG_KEY = "Purpose"
BASTION_TAG_VALUE = "alpha-assessment-bastion"
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
_SECRET_MARKS = ("AKIA", "ASIA", "SecretAccessKey", "SessionToken")


class BastionError(Exception):
    """bastion 사용 실패. 메시지에 비밀값을 넣지 않는다."""


@dataclass
class BastionInfo:
    instance_id: str
    vpc_id: str | None
    launch_time: datetime | None
    ssm_online: bool = False


def find_bastion(session: AwsSession, region: str, vpc_id: str | None) -> BastionInfo | None:
    """태그로 실행 중인 bastion 중 가장 최근 1개를 찾고 SSM Online 여부를 채운다."""
    ec2 = session.client("ec2", region_name=region)
    filters = [
        {"Name": f"tag:{BASTION_TAG_KEY}", "Values": [BASTION_TAG_VALUE]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ]
    if vpc_id:
        filters.append({"Name": "vpc-id", "Values": [vpc_id]})
    found: list[BastionInfo] = []
    token = None
    while True:
        kw: dict = {"Filters": filters}
        if token:
            kw["NextToken"] = token
        res = ec2.describe_instances(**kw)
        for r in res.get("Reservations", []):
            for i in r.get("Instances", []):
                found.append(BastionInfo(i["InstanceId"], i.get("VpcId"), i.get("LaunchTime")))
        token = res.get("NextToken")
        if not token:
            break
    if not found:
        return None
    # 시각이 없는 항목은 가장 오래된 것으로 취급
    best = max(found, key=lambda b: (b.launch_time is not None, b.launch_time.timestamp() if b.launch_time else 0))
    ssm = session.client("ssm", region_name=region)
    res = ssm.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [best.instance_id]}]
    )
    best.ssm_online = any(
        x.get("PingStatus") == "Online" for x in res.get("InstanceInformationList", [])
    )
    return best


def ensure_bastion(
    session: AwsSession, region: str, vpc_id: str | None
) -> tuple[BastionInfo | None, str | None]:
    """(bastion, 실패 사유). 사용 가능하면 사유는 None.

    확장 지점: 'bastion 없음' 분기에서 나중에 프로비저닝(terraform 또는 boto3)을 붙인다.
    """
    try:
        info = find_bastion(session, region, vpc_id)
    except Exception as e:
        return None, f"bastion 조회 실패 ({error_code(e)})"
    if info is None:
        return None, "bastion 없음 (프로비저닝 미구현)"
    if not info.ssm_online:
        return None, f"bastion SSM Online 아님 ({info.instance_id})"
    return info, None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _child_env(session: AwsSession, region: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    env["AWS_DEFAULT_REGION"] = region
    get = getattr(session, "get_credentials", None)
    creds = get() if get else None
    if creds is not None:
        frozen = creds.get_frozen_credentials()
        env["AWS_ACCESS_KEY_ID"] = frozen.access_key
        env["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
        if frozen.token:
            env["AWS_SESSION_TOKEN"] = frozen.token
    return env


def _last_line(proc) -> str:
    """자식 출력의 마지막 한 줄만 (키 의심 줄 제거). 실패 시 빈 문자열."""
    try:
        out = proc.stderr.read(4096) if proc.stderr else b""  # 출력 크기 제한
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
    except Exception:
        return ""
    lines = [l.strip() for l in str(out).splitlines() if l.strip()]
    lines = [l for l in lines if not any(m in l for m in _SECRET_MARKS)]
    return lines[-1][:200] if lines else ""


def _signal_group(proc, sig) -> None:
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and pid > 1:
        try:
            os.killpg(pid, sig)
        except OSError:
            pass


def _posix() -> bool:
    """프로세스 그룹 호출(os.killpg)이 있는 플랫폼(Linux·macOS)인지. Windows 는 False."""
    return hasattr(os, "killpg")


def _stop(proc) -> None:
    # poll() 여부와 무관하게 그룹을 정리한다(aws 가 먼저 죽어도 plugin 자식이 남을 수 있음)
    # aws CLI 는 session-manager-plugin 을 자식으로 띄우므로 프로세스 그룹 전체를 종료한다
    # Windows 처럼 killpg 가 없으면 자식 하나만 terminate → kill 로 정리한다(ponytail: plugin 손자 잔존 가능)
    if _posix():
        _signal_group(proc, signal.SIGTERM)
    else:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    if _posix():
        # 이미 없으면 ProcessLookupError(OSError) 무시. Windows 에는 SIGKILL 이 없어 getattr 로 읽음
        _signal_group(proc, getattr(signal, "SIGKILL", None))
    try:
        proc.kill()
    except OSError:
        pass
    proc.wait()
    for f in (proc.stdout, proc.stderr):  # PIPE fd 누수 방지
        try:
            if f:
                f.close()
        except Exception:
            pass


@contextmanager
def open_tunnel(
    session: AwsSession,
    region: str,
    bastion: BastionInfo,
    remote_host: str,
    remote_port: int,
    local_port: int | None = None,
    ready_timeout: float = 30.0,
) -> Iterator[tuple[str, int]]:
    """SSM 포트포워딩으로 ("localhost", 로컬포트)를 열고, 나갈 때 반드시 닫는다."""
    if not _HOST_RE.match(remote_host or ""):
        raise BastionError("원격 호스트 형식이 올바르지 않음")
    if not (isinstance(remote_port, int) and 0 < remote_port < 65536):
        raise BastionError("원격 포트 범위가 올바르지 않음")
    if local_port is not None and not (isinstance(local_port, int) and 0 < local_port < 65536):
        raise BastionError("로컬 포트 범위가 올바르지 않음")
    if not isinstance(region, str) or not _REGION_RE.match(region):
        raise BastionError("리전 형식이 올바르지 않음")
    if not shutil.which("aws"):
        raise BastionError("aws CLI 없음: AWS CLI v2 를 설치하세요")
    if not shutil.which("session-manager-plugin"):
        raise BastionError("session-manager-plugin 없음: AWS Session Manager Plugin 을 설치하세요")

    port = local_port or _free_port()
    cmd = [
        "aws", "ssm", "start-session", "--region", region, "--target", bastion.instance_id,
        "--document-name", "AWS-StartPortForwardingSessionToRemoteHost",
        "--parameters", f"host={remote_host},portNumber={remote_port},localPortNumber={port}",
    ]
    proc = subprocess.Popen(
        cmd, env=_child_env(session, region), shell=False,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **({"start_new_session": True} if _posix() else {}),  # POSIX 만: 그룹 단위 종료용
    )
    try:
        deadline = time.monotonic() + ready_timeout
        while not _listening(port):
            if proc.poll() is not None:
                why = _last_line(proc)
                raise BastionError("SSM 터널 프로세스가 먼저 종료됨" + (f": {why}" if why else ""))
            if time.monotonic() >= deadline:
                raise BastionError(f"SSM 터널 준비 시간 초과 ({ready_timeout:g}초)")
            time.sleep(0.2)
        yield "localhost", port
    finally:
        _stop(proc)
        for _ in range(10):  # 포트 리슨이 사라질 때까지 잠깐 대기
            if not _listening(port):
                break
            time.sleep(0.2)
