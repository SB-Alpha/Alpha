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
from .worker_client import WorkerClient

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
    def report(self, state: str, progress: int, metrics: dict[str, Any] | None = None, message: str | None = None) -> None: ...


def default_terraform() -> TerraformCli:
    repo_root = Path(__file__).resolve().parents[5]
    workdir = Path(os.getenv("PROVISIONING_TF_DIR") or repo_root / "infra" / "terraform" / "gcp")
    return TerraformCli(workdir, binary=os.getenv("TERRAFORM_BIN", "terraform"))


def resolve_project_id(project_id: str | None) -> str:
    resolved = project_id or os.getenv("GCP_PROJECT_ID")
    if not resolved:
        raise ProvisioningError("GCP 프로젝트 ID 가 없습니다. 입력의 project_id 나 환경변수 GCP_PROJECT_ID 를 지정하세요")
    return resolved


def build_tfvars(target_infra: Mapping[str, Any], *, project_id: str, deploy_app: bool) -> dict[str, Any]:
    values = {key: value for key, value in target_infra.items() if key in KNOWN_VARIABLES and value is not None}
    values.update(project_id=project_id, deploy_app=deploy_app, create_vpn_key=True)
    return values


def _summary(counts: Mapping[str, int]) -> str:
    return f"변경 계획: 추가 {counts['add']}, 변경 {counts['change']}, 삭제 {counts['destroy']}"


class _ApplyProgress:
    ## terraform apply 출력 줄을 보고 진행률을 올린다. 리소스 하나가 끝날 때마다 5%씩 (상한까지).
    def __init__(self, ctx: Reporter, start: int, end: int) -> None:
        self.ctx, self.end, self.progress = ctx, end, start

    def __call__(self, line: str) -> None:
        if _DONE.match(line):
            self.progress = min(self.end, self.progress + 5)
            self.ctx.report("running", self.progress, message=line[:140])
        elif _STARTED.match(line):
            self.ctx.report("running", self.progress, message=line[:140])


def _converge(
    inp: ProvisioningInput | AppDeployInput,
    ctx: Reporter,
    tf: TerraformCli,
    *,
    deploy_app: bool,
) -> tuple[str, dict[str, int], bool, dict[str, Any]]:
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
    # provisioning 단계: 목적지 인프라를 만들고 target 을 돌려준다.
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
    worker: WorkerClient | None = None,
) -> AppDeployOutput:
    # app_deploy 단계: 목적지 앱(Cloud Run)을 배포하고 app_url 을 돌려줌.
    # 앱이 준비되면 Worker 가 그 주소로 보내도록 알림 (update_worker_target).
    worker_client = None
    if inp.update_worker_target and not inp.dry_run:
        worker_client = worker or WorkerClient.from_env()
        if worker_client is None:
            raise ProvisioningError(
                "Worker 주소(환경변수 WORKER_URL)가 없습니다. 지정하거나 입력의 update_worker_target 을 false 로 하세요"
            )

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

    # /health/ready 는 DB 연결까지 본다. 컷오버 전에 DB 에 붙는지 알아야 해서, TF startup probe(health_check_path, /health/live)와 일부러 다르다.
    _wait_healthy(
        app_url.rstrip("/") + "/health/ready",
        ctx,
        timeout_s=inp.health_timeout_s,
        http_get=http_get,
        sleep=sleep,
        clock=clock,
    )
    ctx.report("running", 98, message="앱 상태 확인 완료 (/health/ready 200)")

    if worker_client is not None:
        worker_client.set_target(app_url)
        ctx.report("running", 99, message="Worker 의 GCP 원본 주소를 새 앱 주소로 설정")
    return AppDeployOutput(
        app_url=app_url,
        healthy=True,
        target=target,
        changes=counts,
        applied=applied,
        worker_target_updated=worker_client is not None,
    )
