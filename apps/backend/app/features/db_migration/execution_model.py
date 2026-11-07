"""입력 주입 결과와 클라우드 조회 결과를 공통 실행 모델로 합친다.

환경변수·비밀 저장소·클라우드 API를 읽지 않는 변환이다. 6번 검사는
db/cidrs만 읽고, 7번 VPN 어댑터는 network.provider_fields를 함께 쓴다.
DB 이름·테이블·접속 정책은 이후 단계에서 결정하므로 여기서 만들지 않는다.
이 모델은 10번에서 에이전트에 보낼 JobRequest와 구분한다.
"""

from typing import TYPE_CHECKING, Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, ValidationError

from .run_input import Cloud, InjectedEndpoint, InjectedRun, REGION_PATTERNS
from .schemas import RUN_ID_PATTERN, single_line

if TYPE_CHECKING:
    from .cloud_lookup import FoundEndpoint, FoundRoute


class ExecutionModelError(ValueError):
    """오류에는 필드 구분만 담고 입력값과 조회 응답은 인용하지 않는다."""


def _single_line(value: str) -> str:
    if not single_line(value):
        raise ValueError("must be a non-empty single line without surrounding spaces")
    return value


Text = Annotated[str, AfterValidator(_single_line)]
SecretRef = Annotated[Text, Field(pattern=r"^secret://[A-Za-z0-9][A-Za-z0-9_./-]*$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class ExecutionDatabase(_Model):
    instance: Text
    host: Text
    port: int = Field(ge=1, le=65535)
    version: Text | None
    publicly_accessible: bool | None
    logical_replication: bool | None


class ExecutionNetwork(_Model):
    cidrs: tuple[Text, ...] = Field(min_length=1)
    # 클라우드별 VPN이 해석한다. 공통 변환에서 항목 이름이나 값을 바꾸지 않는다.
    provider_fields: dict[str, JsonValue]


class ExecutionCredentials(_Model):
    cloud: SecretRef
    db_admin: SecretRef


class ExecutionEndpoint(_Model):
    cloud: Cloud
    region: Text
    scope_id: Text
    db: ExecutionDatabase
    network: ExecutionNetwork
    credentials_ref: ExecutionCredentials

    @property
    def cidrs(self) -> tuple[str, ...]:
        """시작 전 검사의 공통 RouteFacts 계약을 제공한다."""
        return self.network.cidrs


class ExecutionModel(_Model):
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    source: ExecutionEndpoint
    target: ExecutionEndpoint
    # 원본 앱 계정은 source.credentials_ref.db_admin이 가리킨다.
    target_app_user: Text


def build_execution_model(injected: InjectedRun, found: "FoundRoute") -> ExecutionModel:
    """6개 경로를 같은 변환으로 처리한다. 성공은 모델, 실패는 ExecutionModelError다."""
    if injected.source.cloud == injected.target.cloud:
        raise ExecutionModelError("source and target must use different clouds")
    source = _endpoint("source", injected.source, found.source)
    target = _endpoint("target", injected.target, found.target)
    try:
        return ExecutionModel(
            run_id=injected.run_id,
            source=source,
            target=target,
            target_app_user=injected.target_app_user,
        )
    except ValidationError:
        raise ExecutionModelError("invalid execution identity or target app account") from None


def _endpoint(side: str, injected: InjectedEndpoint, found: "FoundEndpoint") -> ExecutionEndpoint:
    if (injected.cloud, injected.region) != (found.cloud, found.region):
        raise ExecutionModelError(f"{side}: lookup does not match the requested cloud and region")
    try:
        # 클라우드별로 변환을 나누지 않고 기존 입력 계약의 리전 형식만 확인한다.
        pattern = REGION_PATTERNS.get(injected.cloud)
        if pattern is None or not pattern.fullmatch(injected.region):
            raise ExecutionModelError(f"{side}: unsupported cloud or invalid region")
        return ExecutionEndpoint(
            cloud=injected.cloud,
            region=injected.region,
            scope_id=found.scope_id,
            db=ExecutionDatabase(
                instance=found.db.instance,
                host=found.db.host,
                port=found.db.port,
                version=found.db.version,
                publicly_accessible=found.db.publicly_accessible,
                logical_replication=found.db.logical_replication,
            ),
            network=ExecutionNetwork(cidrs=tuple(found.cidrs), provider_fields=found.network),
            credentials_ref=ExecutionCredentials(cloud=injected.credentials_ref, db_admin=injected.db_admin_ref),
        )
    except ValidationError:
        raise ExecutionModelError(f"{side}: invalid DB, network or credential references") from None
