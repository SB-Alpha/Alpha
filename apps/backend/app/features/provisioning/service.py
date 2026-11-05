# provisioning 단계 진입 함수.
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from .schemas import AppDeployInput, AppDeployOutput, ProvisioningInput, ProvisioningOutput
from .target import TargetBuildError, build_target, find_problems
from .terraform import ProvisioningError, TerraformCli, summarize_plan

DEFAULT_REGION = "asia-northeast3" 

# Terraform 루트 모듈(infra/terraform/gcp)이 선언한 변수들.
KNOWN_VARIABLES = frozenset(
    {
        "region",
        "service_name",
        "source_image",
        "container_image",
        "container_port",
        "cpu",
        "memory",
        "health_check_path",
        "env_vars",
        "secret_refs",
        "db_version",
        "db_name",
        "db_user",
        "db_tier",
        "db_disk_size_gb",
        "db_logical_decoding",
    }
)

# terraform apply 출력 줄을 알아보는 패턴 (진행률 계산용)
#   "module.x.y: Creating..."                    → 시작
#   "module.x.y: Creation complete after 21s"    → 끝
_STARTED = re.compile(r"^\S+: (Creating|Modifying|Destroying)\.\.\.")
_DONE = re.compile(r"^\S+: (Creation|Modifications|Destruction) complete")


class Reporter(Protocol):
    """workflows 의 RunContext 중 우리가 쓰는 부분. (PR #22 의 RunContext.report 와 같은 모양)

    Protocol: "report 라는 함수만 이런 모양으로 있으면 어떤 객체든 받는다" 는 약속.
    덕분에 workflows 코드가 아직 없어도 시험에서는 가짜 객체를 넣어 쓸 수 있다.
    """

    def report(self, state: str, progress: int, metrics: dict[str, Any] | None = None, message: str | None = None) -> None: ...


def default_terraform() -> TerraformCli:
    """Terraform 코드가 있는 폴더(저장소의 infra/terraform/gcp)를 가리키는 실행기.

    parents[5]: 이 파일에서 다섯 단계 위(provisioning → features → app → backend → apps → 저장소 루트).
    다른 곳의 코드를 쓰고 싶으면 환경변수 PROVISIONING_TF_DIR 로 바꿀 수 있다.
    """
    repo_root = Path(__file__).resolve().parents[5]
    workdir = Path(os.getenv("PROVISIONING_TF_DIR") or repo_root / "infra" / "terraform" / "gcp")
    return TerraformCli(workdir, binary=os.getenv("TERRAFORM_BIN", "terraform"))


def resolve_project_id(project_id: str | None) -> str:
    """GCP 프로젝트 ID 를 정한다. 입력에 있으면 그것, 없으면 환경변수 GCP_PROJECT_ID, 둘 다 없으면 오류."""
    resolved = project_id or os.getenv("GCP_PROJECT_ID")
    if not resolved:
        raise ProvisioningError("GCP 프로젝트 ID 가 없습니다. 입력의 project_id 나 환경변수 GCP_PROJECT_ID 를 지정하세요")
    return resolved


def build_tfvars(target_infra: Mapping[str, Any], *, project_id: str, deploy_app: bool) -> dict[str, Any]:
    """target_infra 에서 Terraform 변수 파일(JSON)에 넣을 값을 만든다.

    - Terraform 이 선언한 변수만 넣는다. 값이 없는(None) 키는 빼서 Terraform 쪽 기본값을 쓰게 한다.
    - create_vpn_key 는 항상 true 다. run 과 deploy_app 이 같은 값을 써야 두 번째 apply 가 VPN 키를 지우지 않는다.
      (Terraform 은 변수 값을 기억하지 않아서, 두 번째 실행에서 이 값이 빠지면 "키를 지운다" 는 계획이 나온다)
    """
    values = {key: value for key, value in target_infra.items() if key in KNOWN_VARIABLES and value is not None}
    values.update(project_id=project_id, deploy_app=deploy_app, create_vpn_key=True)
    return values


def _summary(counts: Mapping[str, int]) -> str:
    """진행 보고 문구용. 예: '변경 계획: 추가 2, 변경 0, 삭제 0'"""
    return f"변경 계획: 추가 {counts['add']}, 변경 {counts['change']}, 삭제 {counts['destroy']}"


class _ApplyProgress:
    """terraform apply 출력 줄을 보고 진행률을 올린다. 리소스 하나가 끝날 때마다 5%씩 (상한까지).

    클래스지만 `_ApplyProgress(ctx, 25, 85)` 로 만든 객체를 함수처럼 부를 수 있다 (__call__).
    TerraformCli.apply 가 출력 줄이 나올 때마다 이 객체를 부르고, 그러면 ctx.report 로 진행 상황이 대시보드에 간다.
    """

    def __init__(self, ctx: Reporter, start: int, end: int) -> None:
        self.ctx, self.end, self.progress = ctx, end, start

    def __call__(self, line: str) -> None:
        if _DONE.match(line):  # 리소스 하나가 끝남 → 진행률 5% 증가 (end 를 넘지 않게)
            self.progress = min(self.end, self.progress + 5)
            self.ctx.report("running", self.progress, message=line[:140])
        elif _STARTED.match(line):  # 리소스 하나가 시작됨 → 진행률은 그대로, 무엇을 하는지만 알림
            self.ctx.report("running", self.progress, message=line[:140])


def _converge(
    inp: ProvisioningInput | AppDeployInput,
    ctx: Reporter,
    tf: TerraformCli,
    *,
    deploy_app: bool,
) -> tuple[str, dict[str, int], bool, dict[str, Any]]:
    """run 과 deploy_app 이 함께 쓰는 핵심: 계획 → 삭제 확인 → 적용 → 출력 읽기.

    "인프라를 원하는 상태로 맞춘다(converge)" 는 뜻. 돌려주는 값은
    (project_id, 변경 개수, 실제로 적용했는지, terraform 출력).
    run 은 deploy_app=False, deploy_app 함수는 True 로 부르는 것만 다르다.
    """
    project_id = resolve_project_id(inp.project_id)
    values = build_tfvars(inp.target_infra, project_id=project_id, deploy_app=deploy_app)

    # 임시 폴더에 변수 파일과 계획 파일을 두고, 끝나면 폴더째 자동으로 지워진다 (with 블록을 나가는 순간)
    with tempfile.TemporaryDirectory(prefix="alpha-tf-") as tmp:
        var_file = Path(tmp) / "vars.tfvars.json"
        plan_file = Path(tmp) / "plan.tfplan"
        var_file.write_text(json.dumps(values))

        ctx.report("running", 5, message="terraform 초기화")
        tf.init()

        ctx.report("running", 15, message="변경 계획 확인")
        exit_code = tf.plan(var_file, plan_file)  # 0 = 바뀔 것 없음, 2 = 바뀔 것 있음
        if exit_code == 0:  # 바뀔 것이 없다 (이미 원하는 상태)
            counts, deletions = {"add": 0, "change": 0, "destroy": 0}, []
        else:
            counts, deletions = summarize_plan(tf.show_plan(plan_file))
        ctx.report("running", 25, message=_summary(counts))

        # 안전장치: 계획에 "지우기" 가 들어 있으면 적용하지 않고 멈춘다.
        if deletions and not inp.allow_destroy:
            raise ProvisioningError(
                f"계획에 기존 리소스 삭제 {len(deletions)}개가 들어 있어 중단했습니다 "
                f"(입력의 서비스 이름 등이 지금 인프라와 다른지 확인하세요): {', '.join(deletions[:5])}"
            )

        applied = False
        if exit_code == 2 and not inp.dry_run:
            tf.apply(plan_file, on_line=_ApplyProgress(ctx, 25, 85))
            applied = True

        ctx.report("running", 90, message="terraform 출력 읽기")
        outputs = tf.output_json()
    return project_id, counts, applied, outputs


def run(inp: ProvisioningInput, ctx: Reporter, *, terraform: TerraformCli | None = None) -> ProvisioningOutput:
    """provisioning 단계: 목적지 인프라를 만들고 target 을 돌려준다. 앱(Cloud Run)은 만들지 않는다 (deploy_app=false).

    workflows 가 이 함수를 부른다. terraform 인자는 시험에서 가짜 실행기를 넣으려는 것이고, 평소에는 비워 둔다.
    """
    tf = terraform or default_terraform()
    project_id, counts, applied, outputs = _converge(inp, ctx, tf, deploy_app=False)

    region = inp.target_infra.get("region") or DEFAULT_REGION
    try:
        target = build_target(outputs, run_id=inp.run_id, project_id=project_id, region=region)
    except TargetBuildError as exc:
        raise ProvisioningError(str(exc)) from exc

    # CDC 가 목적지에 닿지 못할 수 있는 설정(SSL 모드, 버전, 대역 겹침)이 있으면 경고로 알림, 막지는 않음.
    problems = find_problems(target)
    if problems:
        ctx.report("running", 95, message="주의: " + "; ".join(problems))
    return ProvisioningOutput(target=target, problems=problems, changes=counts, applied=applied)


def _default_http_get(url: str) -> int:
    """GET 요청을 보내 상태 코드를 돌려준다. 연결 자체가 안 되면 0.

    오류도 예외로 던지지 않고 숫자로 돌려주는 이유: 앱이 막 시작하는 중에는 연결이 안 되는 것이 정상이라서,
    호출하는 쪽(_wait_healthy)이 "아직이면 기다렸다가 다시" 로 처리하기 쉽게 하려는 것.
    """
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310  (우리가 만든 https 주소만 쓴다)
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def _wait_healthy(
    url: str,
    ctx: Reporter,
    *,
    timeout_s: float,
    http_get: Callable[[str], int],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """url 이 200 을 돌려줄 때까지 3초 간격으로 확인한다. timeout_s 가 지나도 안 되면 예외.

    http_get / sleep / clock 을 인자로 받는 이유: 시험에서 진짜 인터넷과 시간을 쓰지 않고
    "503, 503, 200 순서로 대답하고 시간은 1초씩 흐른다" 같은 상황을 흉내 내려는 것.
    """
    deadline = clock() + timeout_s
    attempt = 0
    while True:
        status = http_get(url)
        if status == 200:
            return
        if clock() >= deadline:
            raise ProvisioningError(f"{url} 이(가) {timeout_s:.0f}초 안에 200 을 돌려주지 않았습니다 (마지막 응답 {status})")
        ctx.report("running", min(98, 92 + attempt), message=f"앱 상태 확인 중 (HTTP {status})")
        attempt += 1
        sleep(3)


def deploy_app(
    inp: AppDeployInput,
    ctx: Reporter,
    *,
    terraform: TerraformCli | None = None,
    http_get: Callable[[str], int] = _default_http_get,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> AppDeployOutput:
    """app_deploy 단계: 목적지 앱(Cloud Run)을 배포하고 /health/ready 가 200 이 될 때까지 확인한다.

    run 과 같은 _converge 를 쓰되 deploy_app=True 로 부르는 것만 다르다 (그래서 Cloud Run 이 새로 생긴다).
    workflows 는 db_migration 이 streaming 에 들어가 테이블이 만들어진 뒤에 이 함수를 부른다.
    http_get / sleep / clock 은 시험용으로 바꿔 끼울 수 있게 열어 둔 것이고, 평소에는 기본값을 쓴다.
    """
    tf = terraform or default_terraform()
    project_id, counts, applied, outputs = _converge(inp, ctx, tf, deploy_app=True)

    region = inp.target_infra.get("region") or DEFAULT_REGION
    try:
        target = build_target(outputs, run_id=inp.run_id, project_id=project_id, region=region)
    except TargetBuildError as exc:
        raise ProvisioningError(str(exc)) from exc

    app_url = target["app"]["url"]
    if inp.dry_run:
        return AppDeployOutput(app_url=app_url, healthy=False, target=target, changes=counts, applied=False)
    if not app_url:
        raise ProvisioningError("app_url 출력이 없습니다. 앱이 배포되지 않았습니다")

    _wait_healthy(
        app_url.rstrip("/") + "/health/ready",
        ctx,
        timeout_s=inp.health_timeout_s,
        http_get=http_get,
        sleep=sleep,
        clock=clock,
    )
    ctx.report("running", 99, message="앱 상태 확인 완료 (/health/ready 200)")
    return AppDeployOutput(app_url=app_url, healthy=True, target=target, changes=counts, applied=applied)
