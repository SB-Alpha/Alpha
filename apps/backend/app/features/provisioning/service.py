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

from .image_transfer import DESTINATION_LOGINS, TransferResult, load_aws_credentials, parse_ecr_host, parse_image, transfer_image
from .providers import DEFAULT_PROVIDER, GCP, get_provider, terraform_dir
from .schemas import AppDeployInput, AppDeployOutput, ProvisioningInput, ProvisioningOutput
from .target import TargetBuildError, build_target, find_problems
from .target_secrets import PutSecret, store_target_secrets
from .terraform import ProvisioningError, TerraformCli, summarize_plan
from .worker_client import WorkerClient

# 예전 코드가 이 이름을 읽을 수 있어 GCP 값으로 남겨 둔다. 목적지별 값은 providers.py 에 있다.
DEFAULT_REGION = GCP.default_region
KNOWN_VARIABLES = GCP.known_variables

# terraform apply 출력 줄을 알아보는 패턴 (진행률 계산용)
#   "module.x.y: Creating..."                    → 시작
#   "module.x.y: Creation complete after 21s"    → 끝
_STARTED = re.compile(r"^\S+: (Creating|Modifying|Destroying)\.\.\.")
_DONE = re.compile(r"^\S+: (Creation|Modifications|Destruction) complete")


class Reporter(Protocol):
    def report(self, state: str, progress: int, metrics: dict[str, Any] | None = None, message: str | None = None) -> None: ...


def default_terraform(provider: str = DEFAULT_PROVIDER) -> TerraformCli:
    config = get_provider(provider)
    return TerraformCli(terraform_dir(config), binary=os.getenv("TERRAFORM_BIN", "terraform"))


def resolve_project_id(project_id: str | None, provider: str = DEFAULT_PROVIDER) -> str:
    config = get_provider(provider)
    resolved = project_id or os.getenv(config.scope_env)
    if not resolved:
        raise ProvisioningError(
            f"{config.scope_label} 가 없습니다. 입력의 project_id 나 환경변수 {config.scope_env} 를 지정하세요"
        )
    return resolved


def build_tfvars(
    target_infra: Mapping[str, Any], *, project_id: str, deploy_app: bool, provider: str = DEFAULT_PROVIDER
) -> dict[str, Any]:
    config = get_provider(provider)
    values = {key: value for key, value in target_infra.items() if key in config.known_variables and value is not None}
    values.update({config.scope_variable: project_id, "deploy_app": deploy_app, **config.forced_variables})
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
    prepare: Callable[[TerraformCli], Mapping[str, Any]] | None = None,
) -> tuple[str, dict[str, int], bool, dict[str, Any]]:
    project_id = resolve_project_id(inp.project_id, inp.provider)
    target_infra = inp.target_infra
    adapt = get_provider(inp.provider).adapt
    if adapt is not None: 
        target_infra, notes = adapt(target_infra)
        for note in notes:
            ctx.report("running", 4, message=note)
    values = build_tfvars(target_infra, project_id=project_id, deploy_app=deploy_app, provider=inp.provider)

    # 임시 폴더에 변수 파일과 계획 파일을 두고, 끝나면 폴더째 자동으로 지워진다 (with 블록을 나가는 순간)
    with tempfile.TemporaryDirectory(prefix="alpha-tf-") as tmp:
        var_file = Path(tmp) / "vars.tfvars.json"
        plan_file = Path(tmp) / "plan.tfplan"

        ctx.report("running", 5, message="terraform 초기화")
        tf.init()

        if prepare is not None:
            values = {**values, **prepare(tf)}  # terraform output 은 init 이 끝나야 읽을 수 있다
        var_file.write_text(json.dumps(values))

        ctx.report("running", 15, message="변경 계획 확인")
        exit_code = tf.plan(var_file, plan_file)  # 0 = 바뀔 것 없음, 2 = 바뀔 것 있음
        if exit_code == 0:  
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



def run(
    inp: ProvisioningInput,
    ctx: Reporter,
    *,
    terraform: TerraformCli | None = None,
    put_secret: PutSecret | None = None,
) -> ProvisioningOutput:

    tf = terraform or default_terraform(inp.provider)
    project_id, counts, applied, outputs = _converge(inp, ctx, tf, deploy_app=False)

    region = inp.target_infra.get("region") or get_provider(inp.provider).default_region
    try:
        target = build_target(outputs, run_id=inp.run_id, project_id=project_id, region=region)
    except TargetBuildError as exc:
        raise ProvisioningError(str(exc)) from exc

    # CDC 가 목적지에 닿지 못할 수 있는 설정(SSL 모드, 버전, 대역 겹침)이 있으면 경고로 알림, 막지는 않음.
    problems = find_problems(target)

    # db_migration 이 target.credentials_ref 의 이름표를 resolve() 로 꺼낼 수 있게 값을 저장한다.
    # 계획만 세우는 dry_run 은 저장하지 않는다. 바뀔 것이 없어도(이미 있는 인프라) 이 run 의 이름표로는 저장한다.
    if inp.dry_run:
        ctx.report("running", 92, message="dry_run 이라 목적지 비밀을 저장하지 않습니다")
    else:
        stored, not_stored = store_target_secrets(outputs, run_id=inp.run_id, put=put_secret)
        problems += not_stored
        ctx.report("running", 92, message="목적지 비밀 저장: " + ", ".join(stored))

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


def _output_value(outputs: Mapping[str, Any], name: str) -> Any:
    entry = outputs.get(name)
    return entry.get("value") if isinstance(entry, Mapping) else None



def _image_step(
    inp: AppDeployInput, ctx: Reporter, transfer: Callable[..., TransferResult]
) -> Callable[[TerraformCli], dict[str, Any]] | None:
    """원본 이미지를 목적지 저장소로 복사하는 단계. 복사할 필요가 없거나 할 수 없으면 None 을 돌려준다.

    복사하지 않는 경우: 원본 이미지가 없을 때, 이미지(container_image)를 이미 지정했을 때, dry_run 일 때,
    원본이 ECR 인데 원본 AWS 키 이름표가 없을 때 (이전과 같이 목적지 저장소에 이미지가 이미 있다고 본다).
    """
    infra = inp.target_infra
    source_image = infra.get("source_image")
    if not source_image or infra.get("container_image"):
        return None
    if inp.dry_run:
        ctx.report("running", 2, message="dry_run 이라 이미지 복사를 건너뜁니다")
        return None
    if inp.provider not in DESTINATION_LOGINS:
        ctx.report("running", 2, message=f"목적지 '{inp.provider}' 는 이미지 복사 로그인이 없어 복사를 건너뜁니다 (목적지 저장소에 이미지가 이미 있어야 합니다)")
        return None
    needs_aws_key = parse_ecr_host(parse_image(str(source_image)).registry) is not None
    if needs_aws_key and not inp.source_credentials_ref:
        ctx.report("running", 2, message="원본 AWS 키가 없어 이미지 복사를 건너뜁니다 (목적지 저장소에 이미지가 이미 있어야 합니다)")
        return None
    if needs_aws_key and not str(inp.source_credentials_ref).startswith("secret://"):
        # 조사 모듈은 로컬 개발용 profile:<이름> 도 받는다. 그 이름표는 서버에서 풀 수 없으니 실패시키지 않고 건너뛴다.
        ctx.report("running", 2, message="원본 AWS 키 이름표가 secret:// 형식이 아니라 이미지 복사를 건너뜁니다 (목적지 저장소에 이미지가 이미 있어야 합니다)")
        return None
    service_name = infra.get("service_name")
    if not service_name:
        raise ProvisioningError("service_name 이 없어 복사할 이미지의 목적지 이름을 정할 수 없습니다")

    def prepare(tf: TerraformCli) -> dict[str, Any]:
        outputs = tf.output_json()
        registry_url = _output_value(outputs, "registry_url")
        if not registry_url:
            raise ProvisioningError("registry_url 출력이 없습니다. provisioning 단계가 먼저 끝나 있어야 합니다")
        credentials = load_aws_credentials(inp.source_credentials_ref) if needs_aws_key else None
        result = transfer(
            source_image=str(source_image),
            service_name=str(service_name),
            registry_url=str(registry_url),
            provider=inp.provider,
            aws_credentials=credentials,
            report=lambda message: ctx.report("running", 10, message=message),
        )
        done = "목적지에 있던 이미지를 사용" if getattr(result, "reused", False) else "이미지 복사 완료"
        ctx.report("running", 12, message=f"{done} ({result.digest[:19]})")
        # DR 을 켠 경우 DR 앱은 자기 저장소에서 받으므로 같은 이미지를 DR 저장소에도 복사한다 (digest 는 같다).
        dr_registry_url = _output_value(outputs, "dr_registry_url")
        if dr_registry_url:
            dr_result = transfer(
                source_image=str(source_image),
                service_name=str(service_name),
                registry_url=str(dr_registry_url),
                provider=inp.provider,
                aws_credentials=credentials,
                report=lambda message: ctx.report("running", 13, message=f"DR: {message}"),
            )
            if dr_result.digest != result.digest:
                raise ProvisioningError("DR 저장소에 복사한 이미지의 digest 가 주 저장소와 다릅니다")
            ctx.report("running", 14, message=f"DR 저장소에도 복사 완료 ({dr_result.digest[:19]})")
        return {"container_image": result.container_image}

    return prepare



def deploy_app(
    inp: AppDeployInput,
    ctx: Reporter,
    *,
    terraform: TerraformCli | None = None,
    http_get: Callable[[str], int] = _default_http_get,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    worker: WorkerClient | None = None,
    transfer: Callable[..., TransferResult] = transfer_image,
) -> AppDeployOutput:
  
    worker_client = None
    if inp.update_worker_target and not inp.dry_run:
        worker_client = worker or WorkerClient.from_env()
        if worker_client is None:
            raise ProvisioningError(
                "Worker 주소(환경변수 WORKER_URL)가 없습니다. 지정하거나 입력의 update_worker_target 을 false 로 하세요"
            )

    prepare = _image_step(inp, ctx, transfer)
    tf = terraform or default_terraform(inp.provider)
    project_id, counts, applied, outputs = _converge(inp, ctx, tf, deploy_app=True, prepare=prepare)

    region = inp.target_infra.get("region") or get_provider(inp.provider).default_region
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
