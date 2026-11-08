"""온프렘 입력과 고정 출력. 다음 모듈에는 기존 target 필드만 전달한다."""

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.features.db_migration.schemas import (
    TargetInput,
    TargetDb,
    TargetNetwork,
    TargetApp,
    TargetCredentials,
)
from app.features.provisioning.schemas import AppDeployOutput, ProvisioningOutput

SecretRef = Annotated[str, Field(pattern=r"^secret://[^\s]+$")]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")]


class OnpremConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    deployment_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}$")
    directory: Path
    docker_socket: Path
    database: Identifier
    admin_ref: SecretRef
    app_ref: SecretRef
    cloudflare_ref: SecretRef
    worker_secret_ref: SecretRef
    account_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    zone_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    tunnel_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    db_access_app_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    db_hostname: str
    app_hostname: str
    cdc_db_host: Literal["127.0.0.1"] = "127.0.0.1"
    cdc_db_port: int = Field(default=15432, ge=1, le=65535)
    platform: Literal["linux/amd64", "linux/arm64"]
    allow_emulation: bool = False
    postgres_image: str = "postgres:16.15"
    cloudflared_image: str = "cloudflare/cloudflared:2024.12.2"
    app_secret_refs: dict[str, SecretRef] = Field(default_factory=dict)
    update_worker_target: bool = True
    health_timeout_s: int = Field(default=120, ge=1, le=600)

    @field_validator("directory", "docker_socket")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("온프렘 경로는 .. 없는 절대 경로여야 합니다")
        return value

    @field_validator("db_hostname", "app_hostname")
    @classmethod
    def hostname(cls, value: str) -> str:
        import re
        import ipaddress

        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            raise ValueError("IP 대신 공개 DNS 호스트명을 입력하세요")
        if (
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value)
            or "." not in value
            or ".." in value
        ):
            raise ValueError("소문자 공개 호스트명이 필요합니다")
        if urlsplit("https://" + value).hostname != value:
            raise ValueError("호스트명만 입력하세요")
        return value

    @model_validator(mode="after")
    def separate_routes(self):
        if self.db_hostname == self.app_hostname:
            raise ValueError("DB와 앱 호스트명은 달라야 합니다")
        return self


class FixedDb(TargetDb):
    model_config = ConfigDict(extra="forbid")


class FixedNetwork(TargetNetwork):
    model_config = ConfigDict(extra="forbid")


class FixedApp(TargetApp):
    model_config = ConfigDict(extra="forbid")


class FixedCredentials(TargetCredentials):
    model_config = ConfigDict(extra="forbid")
    cloud: SecretRef
    admin: SecretRef
    ddl_user: SecretRef | None = None
    sink_user: SecretRef | None = None


class OnpremTarget(TargetInput):
    """기존 DB 모듈 TargetInput의 필드 집합을 유지하고 확장 필드를 금지한다."""

    model_config = ConfigDict(extra="forbid")
    cloud: Literal["onprem"] = "onprem"
    db: FixedDb
    network: FixedNetwork = Field(default_factory=FixedNetwork)
    app: FixedApp
    credentials_ref: FixedCredentials


class OnpremProvisioningOutput(ProvisioningOutput):
    model_config = ConfigDict(extra="forbid")
    target: OnpremTarget


class OnpremAppDeployOutput(AppDeployOutput):
    model_config = ConfigDict(extra="forbid")
    target: OnpremTarget

    @model_validator(mode="after")
    def same_url(self):
        if self.app_url != self.target.app.url:
            raise ValueError("app_url과 target.app.url이 일치해야 합니다")
        return self
