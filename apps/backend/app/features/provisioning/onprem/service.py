"""고정 in/out을 반환하는 온프렘 어댑터 서비스. 실제 실행기는 테스트에서 주입 가능하다."""

import re
from copy import deepcopy

from app.core.secrets import resolve
from app.features.provisioning.terraform import ProvisioningError
from app.features.provisioning.worker_client import WorkerClient
from .cloudflare import Cloudflare
from .models import (
    OnpremAppDeployOutput,
    OnpremConfig,
    OnpremProvisioningOutput,
    OnpremTarget,
)
from .runtime import DockerRuntime
from .state import State, atomic_write


def load_config(ref, resolver=resolve):
    if not isinstance(ref, str) or not ref.startswith("secret://"):
        raise ProvisioningError(
            "extra_params.onprem_config_ref에 secret:// 설정 참조가 필요합니다"
        )
    try:
        return OnpremConfig.model_validate_json(resolver(ref)["json"])
    except Exception:
        raise ProvisioningError(
            "온프렘 설정 참조 해석 실패 (필수 입력·형식 확인 필요)"
        ) from None


def secret(ref, fields, resolver):
    try:
        if not isinstance(ref, str) or not ref.startswith("secret://"):
            raise ValueError()
        value = resolver(ref)
        result = {k: value[k] for k in fields}
        if any(
            not isinstance(v, str) or not v or any(ord(ch) < 32 for ch in v)
            for v in result.values()
        ):
            raise ValueError()
        return result
    except Exception:
        raise ProvisioningError(
            "온프렘 Secret 해석 실패 (필수 키: " + ", ".join(fields) + ")"
        ) from None


def credentials(c, resolver):
    admin = secret(c.admin_ref, ("user", "password"), resolver)
    app = secret(c.app_ref, ("user", "password"), resolver)
    for user in (admin["user"], app["user"]):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", user):
            raise ProvisioningError(
                "DB 사용자 이름은 소문자 영문·숫자·밑줄이어야 합니다"
            )
    if admin["user"] == app["user"]:
        raise ProvisioningError("DB 관리자와 앱 계정은 분리해야 합니다")
    return admin, app


def target_for(c, config_ref, app_user):
    # 기존 TargetInput 계약. Docker·Cloudflare·이미지 설정은 여기에 넣지 않는다.
    return OnpremTarget(
        project_id=None,
        region="local",
        db={
            "instance": c.deployment_id,
            "private_ip": c.cdc_db_host,
            "port": c.cdc_db_port,
            "database": c.database,
            "version": "16",
            "ssl_mode": "ENCRYPTED_ONLY",
        },
        network={},
        app={"url": None, "db_user": app_user},
        credentials_ref={"cloud": config_ref, "admin": c.admin_ref},
    )


def provision(
    *, run_id, config_ref, ctx=None, resolver=resolve, runtime=None, edge=None
) -> OnpremProvisioningOutput:
    c = load_config(config_ref, resolver)
    admin, app = credentials(c, resolver)
    cf = secret(c.cloudflare_ref, ("api_token", "tunnel_token"), resolver)
    runtime = runtime or DockerRuntime(c)
    edge = edge or Cloudflare(c, cf["api_token"])
    target = target_for(c, config_ref, app["user"])
    store = State(c)
    with store.lock():
        saved = store.load()
        if saved and saved["target"] != target.model_dump(mode="json"):
            raise ProvisioningError(
                "저장된 target이 다릅니다. 기존 목적지 교체를 거부합니다"
            )
        if ctx:
            ctx.report("running", 10, message="온프렘 DB·Cloudflare 접근 조건 확인")
        # Access는 선행 구성이다. 잘못된 정책이면 DB를 공개하기 전에 중단한다.
        edge.check_db_access()
        runtime.provision(admin, app, cf["tunnel_token"])
        edge.ensure_route(c.db_hostname, "tcp://postgres:5432")
        edge.healthy()
        runtime.probe_cdc()
        store.save(
            saved or {"target": target.model_dump(mode="json"), "deployment": None}
        )
        if ctx:
            ctx.report("running", 100, message="온프렘 목적지 준비·OCI 접속 확인 완료")
    return OnpremProvisioningOutput(
        target=target,
        problems=[],
        changes={"add": 0 if saved else 1, "change": 0, "destroy": 0},
        applied=not bool(saved),
    )


def deploy_app(
    *,
    run_id,
    config_ref,
    target_infra,
    source_credentials_ref,
    ctx=None,
    resolver=resolve,
    runtime=None,
    edge=None,
    worker=None,
) -> OnpremAppDeployOutput:
    c = load_config(config_ref, resolver)
    admin, app = credentials(c, resolver)
    cf = secret(c.cloudflare_ref, ("api_token", "tunnel_token"), resolver)
    worker_secret = secret(c.worker_secret_ref, ("value",), resolver)["value"]
    runtime = runtime or DockerRuntime(c)
    edge = edge or Cloudflare(c, cf["api_token"])
    store = State(c)
    with store.lock():
        saved = store.load()
        if not saved:
            raise ProvisioningError("provision()을 먼저 완료해야 합니다")
        base_target = target_for(c, config_ref, app["user"])
        if saved["target"] != base_target.model_dump(mode="json"):
            raise ProvisioningError("provision() 이후 DB·앱 계정이 변경되었습니다")
        previous = deepcopy(saved.get("deployment"))
        if c.update_worker_target:
            worker = worker or WorkerClient.from_env()
            if worker is None:
                raise ProvisioningError("Worker 목적지 등록 설정이 없습니다")
            status = worker.status()
            if (
                status.get("mode") != "SOURCE"
                or status.get("next_origin", "SOURCE") != "SOURCE"
            ):
                raise ProvisioningError(
                    "Worker가 SOURCE 상태일 때만 목적지 앱을 배포할 수 있습니다"
                )
        source = target_infra.get("source_image")
        if not source or not source_credentials_ref:
            raise ProvisioningError(
                "원본 ECR 이미지 digest와 AWS 자격 증명 참조가 필요합니다"
            )
        env = target_infra.get("env_vars") or {}
        if not isinstance(env, dict) or any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
            or not isinstance(v, str)
            or any(ord(ch) < 32 for ch in v)
            for k, v in env.items()
        ):
            raise ProvisioningError("앱 환경 변수 형식이 올바르지 않습니다")
        # 누락된 앱 Secret을 조용히 배포하지 않는다. 명시한 Secret 참조만 해석한다.
        env = dict(env)
        required_secrets = set((target_infra.get("secret_refs") or {})) | set(
            c.app_secret_refs
        )
        for name in required_secrets:
            if name in {
                "PGHOST",
                "PGPORT",
                "PGDATABASE",
                "PGUSER",
                "PGPASSWORD",
                "PGSSLMODE",
                "WORKER_SECRET",
            }:
                continue
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ProvisioningError("앱 Secret 변수 이름이 올바르지 않습니다")
            ref = c.app_secret_refs.get(name)
            if ref is None:
                raise ProvisioningError("앱 Secret의 온프렘 참조가 없습니다: " + name)
            env[name] = secret(ref, ("value",), resolver)["value"]
        port = target_infra.get("container_port", 8080)
        if port != 8080:
            raise ProvisioningError(
                "현재 온프렘 어댑터는 target-app의 8080 포트 계약을 지원합니다"
            )
        env.update(
            PGHOST="postgres",
            PGPORT="5432",
            PGDATABASE=c.database,
            PGUSER=app["user"],
            PGPASSWORD=app["password"],
            PGSSLMODE="require",
            APP_ENV="onprem",
            PORT="8080",
            WORKER_SECRET=worker_secret,
            REQUIRE_WORKER_SECRET="true",
        )
        if ctx:
            ctx.report("running", 10, message="원본 ECR 이미지를 아카이브로 검증·로드")
        revision = runtime.image(source, source_credentials_ref)
        # Secret 파일은 상태 JSON에 복사하지 않고 로컬 제한 권한 파일로만 보관한다.
        env_path = c.directory / "secrets" / "was.env"
        old_env = env_path.read_text() if env_path.exists() else None
        compose_path = c.directory / "compose.json"
        old_compose = compose_path.read_text() if compose_path.exists() else None
        try:
            runtime.deploy(admin, env, revision["image"])
            runtime.internal_health(worker_secret)
            edge.ensure_route(c.app_hostname, "http://was:8080")
            edge.healthy()
            runtime.external_health(worker_secret)
        except Exception:
            # 기존 배포가 있으면 이미지+환경을 함께 복원. 최초 실패는 WAS만 중지한다.
            try:
                if not previous:
                    # 최초 실패는 기존 compose에 was가 없으므로 먼저 실행 컨테이너를 중지한다.
                    runtime.stop_was()
                if old_env is not None:
                    atomic_write(env_path, old_env)
                else:
                    env_path.unlink(missing_ok=True)
                if old_compose is not None:
                    atomic_write(compose_path, old_compose)
                if previous:
                    runtime.compose(["up", "-d", "--no-deps", "was"])
            except Exception:
                raise ProvisioningError(
                    "온프렘 앱 배포 및 WAS 복구 실패; 수동 확인 필요"
                ) from None
            raise ProvisioningError(
                "온프렘 앱 검증 실패; 이전 WAS 복원 또는 신규 WAS 중지 완료"
            ) from None
        target = base_target.model_copy(deep=True)
        url = "https://" + c.app_hostname
        target.app.url = url
        # 먼저 성공 상태를 기록한다. Worker 오류 후 재시도는 동일 이미지를 다시 검증할 수 있다.
        saved["deployment"] = revision
        store.save(saved)
        updated = False
        if c.update_worker_target:
            worker.set_target(url)  # switch/hold/release를 호출하지 않는다.
            updated = True
        if ctx:
            ctx.report(
                "running", 100, message="온프렘 앱·공개 터널 검증 완료 (트래픽 전환 전)"
            )
        return OnpremAppDeployOutput(
            app_url=url,
            healthy=True,
            target=target,
            changes={
                "add": 0 if previous else 1,
                "change": int(bool(previous)),
                "destroy": 0,
            },
            applied=True,
            worker_target_updated=updated,
            ingress_guard="SECURE",
        )
