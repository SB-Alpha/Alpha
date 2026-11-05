"""terraform 명령을 파이썬에서 실행하는 얇은 껍데기.

비유: 사람이 터미널에 치던 명령(init / plan / apply / show / output)을 대신 쳐 주는 "손" 이다.
판단은 하지 않는다. 판단(삭제가 있으면 멈추기 등)은 service.py 의 몫이다.

하는 일
- 명령을 실행하고, 출력 줄을 한 줄씩 실시간으로 넘겨 준다 (진행 보고용)
- 실패하면 `ProvisioningError` 를 낸다
- 저장된 계획 파일(`plan -out`)을 쓰므로 HCP 워크스페이스가 Local 실행 모드여야 한다 (지금 그렇게 설정돼 있음)
- `terraform output -json` 에는 민감한 값이 평문으로 들어 있다. 이 모듈은 그 값을 로그나 오류 메시지에 싣지 않는다
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

# "한 줄이 들어오면 호출되는 함수" 의 모양. 예: 진행률을 올리는 함수
LineHandler = Callable[[str], None]


class ProvisioningError(RuntimeError):
    """provisioning 단계가 실패했을 때. workflows 는 이 예외를 그 단계의 실패로 기록한다."""


def summarize_plan(plan: Mapping[str, Any]) -> tuple[dict[str, int], list[str]]:
    """`terraform show -json <계획>` 결과에서 (추가·변경·삭제 개수, 삭제되는 리소스 주소 목록)을 뽑는다.

    계획의 각 리소스에는 actions 라는 목록이 있다. 예: ["create"] 만들기, ["update"] 고치기,
    ["delete"] 지우기, ["delete","create"] 교체(지우고 다시 만들기), ["no-op"] 그대로.
    교체는 Terraform 요약과 같이 추가 1, 삭제 1로 센다.
    """
    counts = {"add": 0, "change": 0, "destroy": 0}
    deletions: list[str] = []
    for change in plan.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        if "create" in actions:
            counts["add"] += 1
        if actions == ["update"]:
            counts["change"] += 1
        if "delete" in actions:
            counts["destroy"] += 1
            deletions.append(change.get("address", "?"))  # 어떤 리소스가 지워지는지 오류 메시지에 쓴다
    return counts, deletions


class TerraformCli:
    def __init__(
        self, workdir: Path, *, binary: str = "terraform", timeout_s: float = 1800.0, stop_grace_s: float = 60.0
    ) -> None:
        self.workdir = Path(workdir)  # terraform 을 실행할 폴더 (infra/terraform/gcp)
        self.binary = binary  # 실행 파일 이름. 시험에서는 가짜 스크립트로 바꿔 끼운다
        self.timeout_s = timeout_s  # 이 시간(기본 30분)을 넘기면 끝낸다
        self.stop_grace_s = stop_grace_s  # 중단 신호(SIGINT)를 보낸 뒤 스스로 끝나기를 기다리는 시간. 넘기면 강제 종료

    def _interrupt(self, proc: subprocess.Popen[str]) -> None:
        # terraform 은 SIGINT 를 받으면 진행 중인 작업을 정리하고 상태 잠금을 풀고 끝난다. 강제 종료(kill)는 잠금을 남길 수 있다.
        # 정해진 시간 안에 끝나지 않을 때만 강제로 끝낸다.
        if proc.poll() is not None:
            return
        proc.send_signal(signal.SIGINT)
        force = threading.Timer(self.stop_grace_s, proc.kill)
        force.start()
        try:
            proc.wait()
        finally:
            force.cancel()

    def _env(self) -> dict[str, str]:
        # 현재 환경변수를 그대로 쓰고(HCP·GCP 로그인 정보가 여기서 전달된다), 두 가지만 더한다.
        # TF_IN_AUTOMATION: 자동 실행 중이라고 알려 안내 문구를 줄임 / TF_INPUT=0: 물어보지 말고 진행
        return {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}

    def _stream(self, args: list[str], *, ok_codes: tuple[int, ...] = (0,), on_line: LineHandler | None = None) -> int:
        """명령을 실행하고 출력 줄을 실시간으로 on_line 에 넘긴다. on_line 이 예외를 내면 명령을 중단한다.

        ok_codes: 성공으로 볼 종료 코드. 보통 0 이지만 plan 은 "바뀔 것이 있음"이 2 라서 (0, 2) 를 쓴다.
        """
        try:
            proc = subprocess.Popen(
                [self.binary, *args],
                cwd=self.workdir,
                env=self._env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # 오류 출력도 같은 줄기로 합쳐서 받는다
                text=True,
                bufsize=1,  # 한 줄씩 바로바로 받기
            )
        except FileNotFoundError as exc:
            raise ProvisioningError(f"terraform 실행 파일을 찾을 수 없습니다: {self.binary}") from exc

        # 너무 오래 걸리면 중단 신호를 보내는 알람 (끝나지 않으면 _interrupt 와 같은 유예 시간 뒤 강제 종료)
        timer = threading.Timer(self.timeout_s, self._interrupt, args=(proc,))
        timer.start()
        tail: deque[str] = deque(maxlen=15)  # 실패했을 때 보여 줄 "마지막 15줄" 만 기억한다
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:  # 줄이 나오는 대로 하나씩
                line = raw.rstrip("\n")
                tail.append(line)
                if on_line is not None:
                    on_line(line)  # 예: 중단 요청이 오면 여기서 예외가 나서 아래 except 로 간다
            code = proc.wait()
        except BaseException:
            # 어떤 이유로든 도중에 빠져나가면 terraform 도 같이 멈춘다 (혼자 계속 돌며 인프라를 바꾸지 않도록)
            self._interrupt(proc)
            raise
        finally:
            timer.cancel()
            if proc.stdout is not None:
                proc.stdout.close()

        if code not in ok_codes:
            raise ProvisioningError(f"terraform {args[0]} 실패 (종료 코드 {code}):\n" + "\n".join(tail))
        return code

    def _capture(self, args: list[str]) -> str:
        """JSON 을 돌려주는 명령용. 표준 출력만 돌려주고, 실패 메시지에는 표준 오류만 쓴다(민감한 값 보호)."""
        try:
            result = subprocess.run(
                [self.binary, *args],
                cwd=self.workdir,
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except FileNotFoundError as exc:
            raise ProvisioningError(f"terraform 실행 파일을 찾을 수 없습니다: {self.binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProvisioningError(f"terraform {args[0]} 이(가) {self.timeout_s:.0f}초 안에 끝나지 않았습니다") from exc
        if result.returncode != 0:
            # 표준 출력(stdout)에는 비밀 값이 섞여 있을 수 있어서 오류 메시지에는 표준 오류(stderr)만 쓴다
            stderr_tail = "\n".join(result.stderr.strip().splitlines()[-10:])
            raise ProvisioningError(f"terraform {args[0]} 실패 (종료 코드 {result.returncode}):\n{stderr_tail}")
        return result.stdout

    # -- 사람이 치던 명령들 ---------------------------------------------------

    def init(self, on_line: LineHandler | None = None) -> None:
        """terraform init: 필요한 플러그인과 상태 저장소(HCP) 연결을 준비한다. 이미 되어 있으면 금방 끝난다."""
        self._stream(["init", "-input=false", "-no-color"], on_line=on_line)

    def plan(self, var_file: Path, plan_file: Path, on_line: LineHandler | None = None) -> int:
        """계획을 세워 파일로 저장한다. 돌려주는 값: 0 = 바뀔 것이 없음, 2 = 바뀔 것이 있음 (1 은 오류로 예외).

        -detailed-exitcode: 바뀔 것의 유무를 종료 코드로 알려 주는 옵션
        -out: 계획을 파일로 저장 (나중에 확인한 바로 그 계획만 적용하기 위해)
        """
        return self._stream(
            ["plan", "-input=false", "-no-color", "-detailed-exitcode", f"-var-file={var_file}", f"-out={plan_file}"],
            ok_codes=(0, 2),
            on_line=on_line,
        )

    def show_plan(self, plan_file: Path) -> dict[str, Any]:
        """저장한 계획을 JSON 으로 읽는다 (어떤 리소스가 만들어지고 지워지는지 보려고)."""
        return json.loads(self._capture(["show", "-json", str(plan_file)]))

    def apply(self, plan_file: Path, on_line: LineHandler | None = None) -> None:
        """저장해 둔 계획을 그대로 적용한다 (확인한 계획만 적용되도록)."""
        self._stream(["apply", "-input=false", "-no-color", str(plan_file)], on_line=on_line)

    def output_json(self) -> dict[str, Any]:
        """terraform output -json: 인프라가 만든 결과(DB 주소 등)를 읽는다. 민감한 값이 평문으로 들어 있으니 조심해서 다룬다."""
        return json.loads(self._capture(["output", "-json"]))
