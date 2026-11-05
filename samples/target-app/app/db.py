"""PostgreSQL connection pool.

Connection settings come from the standard libpq environment variables
(PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD, PGSSLMODE), so no custom
URL parsing is needed and ECS can inject PGPASSWORD from a secret.
"""

import logging
from pathlib import Path

from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

pool = ConnectionPool(
    conninfo="",
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row, "autocommit": True},
    open=False,
)


def open_pool() -> None:
    pool.open(wait=True, timeout=30)


def close_pool() -> None:
    pool.close()


def apply_schema() -> None:
    """Create the table if missing. An account without CREATE (PG15+ default on
    `public`) is expected when the schema was created separately or arrived via CDC."""
    try:
        with pool.connection() as conn:
            conn.execute(SCHEMA_PATH.read_text())
    except InsufficientPrivilege:
        logging.getLogger("target-app").warning(
            "schema not applied: no CREATE privilege; assuming work_items already exists"
        )
