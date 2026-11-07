"""10.1번: 9번 조회 결과에서 이전할 테이블을 확정하고 에이전트에 보낼 작업 요청과 계정 이름표를 만든다.

6개 경로 공통이다. 비밀값은 요청에 넣지 않고, DB 안의 계정 생성은 에이전트의 11·12번 절차가 맡는다.
"""

import re
import secrets
from dataclasses import dataclass

from app.core.secrets import put_secret, resolve

from .catalog import CatalogExecution, CatalogTable
from .execution_model import ExecutionModel
from .schemas import JobRequest, SourceSpec, TargetSpec
from .settings import tunables

# 마커 테이블은 에이전트가 관리한다. 복제 대상에 넣지 않는다.
INTERNAL_SCHEMA = "alpha_cdc"
# 에이전트의 작업 요청이 받는 테이블 이름 형식이다. 복제 슬롯과 토픽 이름에도 쓰인다.
_TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
_NAMES_SHOWN = 5


class JobRequestError(RuntimeError):
    """code는 호출자의 분기용이다. 메시지에는 테이블 이름만 담고 비밀값은 담지 않는다."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class JobAccountRefs:
    source_admin: str
    source_repl: str
    target_admin: str
    target_ddl: str
    target_sink: str


def _full_name(table: CatalogTable) -> str:
    return f"{table.schema_name}.{table.name}"


def _refuse_if_any(code: str, problem: str, tables: list[CatalogTable]) -> None:
    if tables:
        names = [_full_name(table) for table in tables]
        more = f" and {len(names) - _NAMES_SHOWN} more" if len(names) > _NAMES_SHOWN else ""
        raise JobRequestError(code, f"{problem}: {', '.join(names[:_NAMES_SHOWN])}{more}")


def select_tables(catalog: CatalogExecution) -> list[str]:
    """원본의 사용자 테이블 전부를 옮긴다. 옮길 수 없는 테이블을 조용히 빼면 데이터가 빠지므로 거절한다."""
    tables = [table for table in catalog.source.tables if table.schema_name != INTERNAL_SCHEMA]
    _refuse_if_any("no_primary_key", "source tables need a primary key",
                   [table for table in tables if not table.primary_key])
    _refuse_if_any("table_not_readable", "the source account cannot read",
                   [table for table in tables if not table.can_select])
    _refuse_if_any("partitioned_table", "partitioned tables are not supported",
                   [table for table in tables if table.kind == "partitioned" or table.is_partition])
    _refuse_if_any("invalid_table_name", "table names must be lowercase",
                   [table for table in tables if not _TABLE_NAME.fullmatch(_full_name(table))])
    if not tables:
        raise JobRequestError("no_source_tables", "the source database has no tables to migrate")
    return [_full_name(table) for table in tables]


def build_request(job_id: str, catalog: CatalogExecution) -> dict:
    """에이전트가 쓰는 값만 꺼낸다. 새 입구의 source.db 계정은 앱이 쓰는 계정이다."""
    execution = catalog.execution
    return JobRequest(
        job_id=job_id,
        source=SourceSpec(
            host=execution.source.db.host,
            port=execution.source.db.port,
            database=catalog.source.database,
            tables=select_tables(catalog),
            app_role=resolve(execution.source.credentials_ref.db_admin)["user"],
        ),
        target=TargetSpec(
            host=execution.target.db.host,
            port=execution.target.db.port,
            database=catalog.target.database,
            app_role=execution.target_app_user,
        ),
    ).model_dump()


def ensure_account(ref: str, user: str) -> dict[str, str]:
    """저장된 계정을 돌려준다. 없으면 만든다. 다시 실행해도 비밀번호가 바뀌지 않는다."""
    try:
        return resolve(ref)
    except KeyError:
        value = {"user": user, "password": secrets.token_hex(24)}
        put_secret(ref, value)
        return value


def ensure_account_refs(execution: ExecutionModel) -> JobAccountRefs:
    """복제·DDL·Sink 계정은 이 run이 만들고, 관리자 계정은 입력에서 받은 것을 쓴다."""
    run_id = execution.run_id
    settings = tunables()
    made = {}
    for name, tag, user in (
        ("source_repl", "source-repl", settings.repl_user),
        ("target_ddl", "target-ddl-user", settings.target_ddl_user),
        ("target_sink", "target-sink-user", settings.target_sink_user),
    ):
        ref = f"secret://{run_id}/{tag}"
        ensure_account(ref, user)
        made[name] = ref
    return JobAccountRefs(
        source_admin=execution.source.credentials_ref.db_admin,
        target_admin=execution.target.credentials_ref.db_admin,
        **made,
    )
