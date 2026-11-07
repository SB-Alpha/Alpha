"""PostgreSQL catalog reads before a job exists. No cloud APIs, DDL or stored secrets."""

from typing import Literal

import psycopg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

router = APIRouter()
MAX_DATABASES = 1000
MAX_TABLES = 10000
SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]


class CatalogError(RuntimeError):
    pass


class CatalogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    host: str = Field(min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    sslmode: SslMode = "require"
    user: SecretStr
    password: SecretStr
    # postgres is only the catalog entry point, never a guessed migration destination.
    bootstrap_database: str = "postgres"
    database: str | None = None

    @field_validator("host", "bootstrap_database", "database", "user", "password")
    @classmethod
    def single_line(cls, value):
        text = value.get_secret_value() if isinstance(value, SecretStr) else value
        if text is not None and (not text or text != text.strip() or any(c in text for c in "\r\n\0")):
            raise ValueError("must be a non-empty single line without surrounding spaces")
        return value


class CatalogTable(BaseModel):
    schema_name: str
    name: str
    primary_key: list[str]
    kind: Literal["table", "partitioned"]
    is_partition: bool
    can_select: bool


class CatalogResult(BaseModel):
    database: str | None
    databases: list[str]
    tables: list[CatalogTable]


DATABASES_SQL = """
SELECT datname FROM pg_catalog.pg_database
WHERE NOT datistemplate AND datallowconn AND datconnlimit <> -2
  AND pg_catalog.has_database_privilege(oid, 'CONNECT')
ORDER BY datname LIMIT 1001
"""

TABLES_SQL = """
SELECT n.nspname, c.relname, c.relkind, c.relispartition,
       pg_catalog.has_schema_privilege(n.oid, 'USAGE')
         AND pg_catalog.has_table_privilege(c.oid, 'SELECT'),
       ARRAY(
           SELECT a.attname FROM pg_catalog.pg_index i
           CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, position)
           JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
           WHERE i.indrelid = c.oid AND i.indisprimary AND i.indisvalid
             AND k.position <= i.indnkeyatts
           ORDER BY k.position
       )
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname !~ '^pg_'
  AND n.nspname NOT IN ('information_schema', 'alpha_cdc')
  AND NOT EXISTS (
      SELECT 1 FROM pg_catalog.pg_depend d
      WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e'
  )
ORDER BY n.nspname, c.relname LIMIT 10001
"""


def inspect_catalog(query: CatalogRequest) -> CatalogResult:
    """A selected DB returns its tables; an unselected request returns connectable DB names."""
    try:
        with psycopg.connect(
            host=query.host, port=query.port, dbname=query.database or query.bootstrap_database,
            user=query.user.get_secret_value(), password=query.password.get_secret_value(),
            sslmode=query.sslmode, connect_timeout=5, autocommit=True,
            application_name="alpha-cdc-catalog",
            options="-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000",
        ) as conn:
            if query.database is None:
                names = [row[0] for row in conn.execute(DATABASES_SQL).fetchall()]
                if len(names) > MAX_DATABASES:
                    raise CatalogError("too many databases; catalog result was not truncated")
                return CatalogResult(database=None, databases=names, tables=[])
            rows = conn.execute(TABLES_SQL).fetchall()
            if len(rows) > MAX_TABLES:
                raise CatalogError("too many tables; catalog result was not truncated")
            tables = [CatalogTable(schema_name=schema, name=name, kind="partitioned" if kind == "p" else "table",
                                   is_partition=partition, can_select=readable, primary_key=pk)
                      for schema, name, kind, partition, readable, pk in rows]
            return CatalogResult(database=query.database, databases=[], tables=tables)
    except CatalogError:
        raise
    except Exception:
        # Driver errors can contain the host, role, password or server response.
        raise CatalogError("cannot read PostgreSQL catalog; check connectivity, account and database access") from None


@router.post("/catalog", response_model=CatalogResult)
async def catalog(request: Request) -> CatalogResult:
    # FastAPI's default validation error includes the submitted input. Keep this route's credentials out of it.
    try:
        query = CatalogRequest.model_validate(await request.json())
    except (ValueError, ValidationError):
        raise HTTPException(422, "invalid catalog request") from None
    try:
        return await run_in_threadpool(inspect_catalog, query)
    except CatalogError as exc:
        raise HTTPException(502, str(exc)) from None
