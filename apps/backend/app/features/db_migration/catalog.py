"""9번: VPN과 접속 확인 뒤 OCI 에이전트에서 DB 이름·테이블을 조회한다.

discover_catalog(execution, agent=agent, selection=CatalogSelection(...))
후보가 여러 개면 DatabaseSelectionRequired.candidates를 보고 명시적으로 선택한다.
postgres는 목록 조회의 기본 접속 DB일 뿐 이전할 DB로 간주하지 않는다.
공통 실행 모델에 조회 결과를 묶어 10번에 전달한다. 작업 등록과 DDL은 하지 않는다.
"""

from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.secrets import resolve

from .execution_model import ExecutionEndpoint, ExecutionModel, Text
from .schemas import single_line

SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]


class CatalogError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DatabaseSelectionRequired(CatalogError):
    def __init__(self, candidates: dict[str, tuple[str, ...]]):
        super().__init__("database_selection_required", "multiple databases require an explicit source and target selection")
        self.candidates = candidates


class CatalogReader(Protocol):
    def catalog(self, query: dict) -> dict: ...


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class CatalogSelection(_Model):
    source_database: Text | None = None
    target_database: Text | None = None


class CatalogOptions(_Model):
    sslmode: SslMode = "require"
    source_bootstrap_database: Text = "postgres"
    target_bootstrap_database: Text = "postgres"


class CatalogTable(_Model):
    schema_name: Text
    name: Text
    primary_key: list[Text]
    kind: Literal["table", "partitioned"]
    is_partition: bool = Field(strict=True)
    can_select: bool = Field(strict=True)


class _Response(_Model):
    database: Text | None
    databases: list[Text] = Field(max_length=1000)
    tables: list[CatalogTable] = Field(max_length=10000)

    @model_validator(mode="after")
    def consistent(self):
        if self.database is None and self.tables or self.database is not None and self.databases:
            raise ValueError("mixed database list and table catalog")
        if len(set(self.databases)) != len(self.databases):
            raise ValueError("duplicate database names")
        keys = [(t.schema_name, t.name) for t in self.tables]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate tables")
        return self


class DatabaseCatalog(_Model):
    database: Text
    tables: list[CatalogTable]


class CatalogExecution(_Model):
    execution: ExecutionModel
    source: DatabaseCatalog
    target: DatabaseCatalog


def discover_catalog(
    execution: ExecutionModel, *, agent: CatalogReader, selection: CatalogSelection | None = None,
    options: CatalogOptions | None = None, stop_requested: Callable[[], bool] | None = None,
) -> CatalogExecution:
    """6개 경로 공통. 계정은 보내는 순간만 해석하고 결과에는 남기지 않는다."""
    selection = selection or CatalogSelection()
    options = options or CatalogOptions()

    def check_stop():
        if stop_requested is not None and stop_requested():
            raise CatalogError("stopped", "catalog discovery was stopped before handoff")

    def read(side: str, database: str | None) -> _Response:
        check_stop()
        endpoint: ExecutionEndpoint = getattr(execution, side)
        try:
            account = resolve(endpoint.credentials_ref.db_admin)
            if not all(isinstance(account.get(k), str) and single_line(account[k]) for k in ("user", "password")):
                raise ValueError
            response = agent.catalog({
                "host": endpoint.db.host, "port": endpoint.db.port, "sslmode": options.sslmode,
                "user": account["user"], "password": account["password"], "database": database,
                "bootstrap_database": getattr(options, f"{side}_bootstrap_database"),
            })
        except Exception:
            raise CatalogError("catalog_unavailable", f"{side}: cannot read catalog; check agent, DB account and connectivity") from None
        check_stop()
        try:
            result = _Response.model_validate(response)
            if result.database != database:
                raise ValueError
        except (ValidationError, ValueError):
            raise CatalogError("invalid_catalog_response", f"{side}: agent returned an invalid catalog response") from None
        return result

    candidates = {side: tuple(read(side, None).databases) for side in ("source", "target")}
    chosen = {}
    for side, names in candidates.items():
        if not names:
            raise CatalogError("no_database", f"{side}: no connectable non-template database")
        supplied = getattr(selection, f"{side}_database")
        if supplied is not None and supplied not in names:
            raise CatalogError("invalid_database_selection", f"{side}: selected database is not in the catalog")
        chosen[side] = supplied or (names[0] if len(names) == 1 else None)
    if any(name is None for name in chosen.values()):
        raise DatabaseSelectionRequired(candidates)
    catalogs = {side: read(side, chosen[side]) for side in ("source", "target")}
    if not catalogs["source"].tables:
        raise CatalogError("no_source_tables", "source: selected database has no user tables")
    check_stop()
    return CatalogExecution(
        execution=execution,
        source=DatabaseCatalog(database=catalogs["source"].database, tables=catalogs["source"].tables),
        target=DatabaseCatalog(database=catalogs["target"].database, tables=catalogs["target"].tables),
    )
