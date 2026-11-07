"""run 입력의 입구. 입력을 검증하고, 클라우드 권한을 찾고, DB 계정을 이름표로 바꾼다.

클라우드 API, VPN, 복제는 여기서 하지 않는다. 이 파일을 지나면 비밀값은 이름표로만 다닌다.
"""

import re
import threading
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from app.core.secrets import has_secret, put_secret, resolve

from .schemas import RUN_ID_PATTERN, single_line

Cloud = Literal["aws", "gcp", "azure"]

CLOUD_CREDENTIAL_KEYS: dict[str, frozenset[str]] = {
    "aws": frozenset({"aws_access_key_id", "aws_secret_access_key"}),
    "gcp": frozenset({"credentials_json"}),
    "azure": frozenset({"tenant_id", "client_id", "client_secret", "subscription_id"}),
}
REGION_PATTERNS: dict[str, re.Pattern[str]] = {
    "aws": re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$"),
    "gcp": re.compile(r"^[a-z]+-[a-z]+\d+$"),
    "azure": re.compile(r"^[a-z]+\d*$"),
}
SOURCE_DB_TAG = "source-db-admin"
TARGET_DB_TAG = "target-admin"
# 정리 시점에는 run_id만 알므로 안내 분기에 쓸 cloud 값을 남긴다. 비밀값은 없다.
CLOUDS_TAG = "run-clouds"
UNKNOWN_FIELD = "unknown_field"
# 저장소는 이름으로 찾을 수 없어 화면에 보여 줄 run을 따로 적어 둔다. 비밀값은 없다.
RUN_INDEX = "db-migration/run-index"
_index_lock = threading.Lock()


class InvalidInput(ValueError):
    """필드 이름만 담는다. pydantic 원문은 입력값을 인용할 수 있다."""

    def __init__(self, fields: list[str]):
        super().__init__(f"invalid input: {', '.join(fields)}")
        self.fields = fields


class MissingCredentials(RuntimeError):
    """클라우드 권한이 저장되지 않았거나 형식이 맞지 않는다. 값과 저장된 키 이름은 담지 않는다."""


class NoSavedInput(LookupError):
    """입력을 다시 받지 않고는 이어서 실행할 수 없다."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DbAccount(_Strict):
    user: SecretStr
    password: SecretStr
    # 서버에 DB가 하나면 생략한다. 여러 개면 조회 결과를 보고 이 값으로 고른다.
    database: str | None = None

    @field_validator("user", "password")
    @classmethod
    def _single_line_secret(cls, value: SecretStr) -> SecretStr:
        if not single_line(value.get_secret_value()):
            raise ValueError("must be a single line of text without surrounding spaces")
        return value

    @field_validator("database")
    @classmethod
    def _single_line_database(cls, value: str | None) -> str | None:
        if value is not None and not single_line(value):
            raise ValueError("must be a single line of text without surrounding spaces")
        return value


class TargetDbAccount(DbAccount):
    app_user: str

    @field_validator("app_user")
    @classmethod
    def _single_line_name(cls, value: str) -> str:
        if not single_line(value):
            raise ValueError("must be a single line of text without surrounding spaces")
        return value


class _Endpoint(_Strict):
    cloud: Cloud
    region: str

    @field_validator("region")
    @classmethod
    def _region_of_cloud(cls, value: str, info: ValidationInfo) -> str:
        pattern = REGION_PATTERNS.get(info.data.get("cloud", ""))
        if pattern is not None and not pattern.fullmatch(value):
            raise ValueError("not a region of this cloud")
        return value


class SourceEndpoint(_Endpoint):
    db: DbAccount


class TargetEndpoint(_Endpoint):
    db: TargetDbAccount


class RunInput(_Strict):
    run_id: str | None = None
    source: SourceEndpoint
    target: TargetEndpoint

    @field_validator("run_id")
    @classmethod
    def _run_id_format(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(RUN_ID_PATTERN, value):
            raise ValueError("not a run id")
        return value


@dataclass(frozen=True)
class InjectedEndpoint:
    cloud: str
    region: str
    credentials_ref: str
    db_admin_ref: str


@dataclass(frozen=True)
class InjectedRun:
    """입력 주입의 결과. 이름표만 담으므로 로그와 스레드로 넘겨도 된다."""

    run_id: str
    source: InjectedEndpoint
    target: InjectedEndpoint
    target_app_user: str
    source_database: str | None = None
    target_database: str | None = None


def input_problems(exc: ValidationError) -> list[str]:
    return sorted({_field_name(error) for error in exc.errors()})


def parse(raw: Any) -> RunInput:
    try:
        parsed = RunInput.model_validate(raw)
    except ValidationError as exc:
        raise InvalidInput(input_problems(exc)) from None
    if parsed.source.cloud == parsed.target.cloud:
        raise InvalidInput(["target.cloud"])
    return parsed


def inject(run_id: str, inp: RunInput) -> InjectedRun:
    """권한을 모두 확인한 뒤에 DB 계정을 저장한다. 같은 run_id로 다시 부르면 계정을 덮어쓴다."""
    source_credentials = _credentials_ref(inp.source.cloud)
    target_credentials = _credentials_ref(inp.target.cloud)
    put_secret(
        f"{run_id}/{CLOUDS_TAG}",
        _without_none({
            "source": inp.source.cloud,
            "target": inp.target.cloud,
            "source_region": inp.source.region,
            "target_region": inp.target.region,
            "target_app_user": inp.target.db.app_user,
            "source_database": inp.source.db.database,
            "target_database": inp.target.db.database,
        }),
    )
    _remember_run(run_id)
    return InjectedRun(
        run_id=run_id,
        source=_inject_endpoint(run_id, inp.source, source_credentials, SOURCE_DB_TAG),
        target=_inject_endpoint(run_id, inp.target, target_credentials, TARGET_DB_TAG),
        target_app_user=inp.target.db.app_user,
        source_database=inp.source.db.database,
        target_database=inp.target.db.database,
    )


def reload_run(run_id: str, *, source_database: str | None = None, target_database: str | None = None) -> InjectedRun:
    """저장된 입력으로 다시 실행한다. DB 이름을 주면 그 값으로 바꾸고, 주지 않으면 전에 고른 값을 쓴다."""
    try:
        saved = resolve(f"secret://{run_id}/{CLOUDS_TAG}")
    except KeyError:
        raise NoSavedInput(run_id) from None
    source_ref, target_ref = f"secret://{run_id}/{SOURCE_DB_TAG}", f"secret://{run_id}/{TARGET_DB_TAG}"
    if "target_app_user" not in saved or not has_secret(source_ref) or not has_secret(target_ref):
        raise NoSavedInput(run_id)
    source_credentials = _credentials_ref(saved["source"])
    target_credentials = _credentials_ref(saved["target"])
    source_database = source_database or saved.get("source_database")
    target_database = target_database or saved.get("target_database")
    put_secret(f"{run_id}/{CLOUDS_TAG}",
               _without_none({**saved, "source_database": source_database, "target_database": target_database}))
    return InjectedRun(
        run_id=run_id,
        source=InjectedEndpoint(saved["source"], saved["source_region"], source_credentials, source_ref),
        target=InjectedEndpoint(saved["target"], saved["target_region"], target_credentials, target_ref),
        target_app_user=saved["target_app_user"],
        source_database=source_database,
        target_database=target_database,
    )


def save_target_network(run_id: str, network: str | None) -> None:
    """앱을 다시 켜도 상태에 나오도록 run과 함께 남긴다."""
    if network:
        put_secret(f"{run_id}/{CLOUDS_TAG}", {**resolve(f"secret://{run_id}/{CLOUDS_TAG}"), "target_db_network": network})


def saved_target_network(run_id: str) -> str | None:
    try:
        return resolve(f"secret://{run_id}/{CLOUDS_TAG}").get("target_db_network")
    except KeyError:
        return None


def known_runs() -> list[str]:
    try:
        return list(resolve(f"secret://{RUN_INDEX}"))
    except KeyError:
        return []


def forget_runs(run_ids: list[str]) -> None:
    with _index_lock:
        put_secret(RUN_INDEX, {run_id: "1" for run_id in known_runs() if run_id not in run_ids})


def credentials_registered(cloud: str) -> bool:
    try:
        _credentials_ref(cloud)
    except (MissingCredentials, RuntimeError):  # RuntimeError: 지금 키로 복호화할 수 없다
        return False
    return True


def saved_clouds(run_id: str) -> tuple[str, str] | None:
    """이 입구로 시작한 run의 (원본, 목적지) 클라우드. 다른 경로로 시작했으면 None."""
    try:
        clouds = resolve(f"secret://{run_id}/{CLOUDS_TAG}")
    except KeyError:
        return None
    return clouds["source"], clouds["target"]


def saved_route(run_id: str) -> dict[str, dict[str, str]] | None:
    """이 입구로 시작한 run의 경로. 다른 경로로 시작했으면 None."""
    try:
        saved = resolve(f"secret://{run_id}/{CLOUDS_TAG}")
        return {
            "source": {"cloud": saved["source"], "region": saved["source_region"]},
            "target": {"cloud": saved["target"], "region": saved["target_region"]},
        }
    except KeyError:
        return None


def _without_none(values: dict[str, str | None]) -> dict[str, str]:
    return {key: value for key, value in values.items() if value is not None}


def _remember_run(run_id: str) -> None:
    with _index_lock:
        put_secret(RUN_INDEX, {**dict.fromkeys(known_runs(), "1"), run_id: "1"})


def _field_name(error: Any) -> str:
    # 모르는 필드는 이름도 값일 수 있어 위치만 남긴다.
    location = error["loc"][:-1] + (UNKNOWN_FIELD,) if error["type"] == "extra_forbidden" else error["loc"]
    return ".".join(str(part) for part in location) or "input"


def _credentials_ref(cloud: str) -> str:
    ref = f"secret://cloud/{cloud}"
    try:
        stored = resolve(ref)
    except KeyError:
        raise MissingCredentials(f"credentials for {cloud} are not registered") from None
    expected = CLOUD_CREDENTIAL_KEYS[cloud]
    if stored.keys() != expected or not all(stored.values()):
        raise MissingCredentials(f"credentials for {cloud} must have exactly: {', '.join(sorted(expected))}")
    return ref


def _inject_endpoint(
    run_id: str, endpoint: SourceEndpoint | TargetEndpoint, credentials_ref: str, db_tag: str
) -> InjectedEndpoint:
    db_admin_ref = put_secret(f"{run_id}/{db_tag}", {
        "user": endpoint.db.user.get_secret_value(),
        "password": endpoint.db.password.get_secret_value(),
    })
    return InjectedEndpoint(
        cloud=endpoint.cloud,
        region=endpoint.region,
        credentials_ref=credentials_ref,
        db_admin_ref=db_admin_ref,
    )
