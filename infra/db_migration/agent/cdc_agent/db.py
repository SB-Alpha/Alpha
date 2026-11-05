"""PostgreSQL work done by the agent on the source and target databases.

Accounts (from /etc/alpha-cdc/secrets/<job_id>.properties):
  source.user / source.password          replication account (snapshot, WAL, marker insert)
  source.app.user / source.app.password  app account, used only to block its own writes at cutover
  source.admin.user / source.admin.password  master account, prepares and cleans up the source
  target.user / target.password          sink account (DML on target tables)
  target.ddl.user / target.ddl.password  schema account (creates target tables)
  target.admin.user / target.admin.password  admin account, creates the two target accounts
"""

import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg
from psycopg import sql

from cdc_agent.models import MARKER_TABLE, Job


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    database: str
    sslmode: str
    user: str
    password: str

    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "PGHOST": self.host,
            "PGPORT": str(self.port),
            "PGDATABASE": self.database,
            "PGUSER": self.user,
            "PGPASSWORD": self.password,
            "PGSSLMODE": self.sslmode,
        }


@contextmanager
def connect(ep: Endpoint) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(
        host=ep.host, port=ep.port, dbname=ep.database, user=ep.user,
        password=ep.password, sslmode=ep.sslmode, autocommit=True, connect_timeout=5,
        application_name="alpha-cdc-agent",
    )
    try:
        # Same text form of timestamptz on both sides for checksums.
        conn.execute("SET TIME ZONE 'UTC'")
        yield conn
    finally:
        conn.close()


def split(table: str) -> tuple[str, str]:
    schema, name = table.split(".", 1)
    return schema, name


def ident(table: str) -> sql.Identifier:
    return sql.Identifier(*split(table))


def table_exists(conn: psycopg.Connection, table: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()[0]


def primary_key(conn: psycopg.Connection, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def schemas_of(tables: list[str]) -> list[str]:
    return sorted({split(t)[0] for t in tables})


def role_exists(conn: psycopg.Connection, role: str) -> bool:
    return conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is not None


def set_login(conn: psycopg.Connection, user: str, password: str) -> None:
    # Send a verifier, not the password: the server may log DDL statements.
    verifier = conn.pgconn.encrypt_password(password.encode(), user.encode(), b"scram-sha-256").decode()
    verb = "ALTER" if role_exists(conn, user) else "CREATE"
    conn.execute(sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(user), sql.Literal(verifier)))


def prepare_target(admin: Endpoint, ddl: Endpoint, sink: Endpoint) -> None:
    """The schema and sink accounts. Made by SQL so Cloud SQL does not add them to its admin group.
    Safe to run again."""
    database = sql.Identifier(admin.database)
    ddl_role, sink_role = sql.Identifier(ddl.user), sql.Identifier(sink.user)
    with connect(admin) as conn:
        set_login(conn, ddl.user, ddl.password)
        set_login(conn, sink.user, sink.password)
        conn.execute(sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(database, ddl_role))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, sink_role))
        conn.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(ddl_role))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sink_role))


def prepare_source(admin: Endpoint, job: Job, repl_user: str, repl_password: str) -> None:
    """Replication account, marker table and publication. Safe to run again."""
    repl = sql.Identifier(repl_user)
    with connect(admin) as conn:
        # After a cutover the master is read-only by role default; a session can lift that.
        conn.execute("SET default_transaction_read_only = off")
        set_login(conn, repl_user, repl_password)
        # RDS grants replication through a role; plain PostgreSQL through an attribute.
        if role_exists(conn, "rds_replication"):
            conn.execute(sql.SQL("GRANT rds_replication TO {}").format(repl))
        else:
            conn.execute(sql.SQL("ALTER ROLE {} REPLICATION").format(repl))
        for schema in schemas_of(job.request.source.tables):
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), repl))
        for table in job.request.source.tables:
            conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(ident(table), repl))
        marker_schema, _ = split(MARKER_TABLE)
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(marker_schema)))
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} (id uuid PRIMARY KEY, job_id text NOT NULL, "
            "created_at timestamptz NOT NULL DEFAULT now())").format(ident(MARKER_TABLE)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(marker_schema), repl))
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(ident(MARKER_TABLE), repl))
        published = conn.execute(
            "SELECT 1 FROM pg_publication WHERE pubname = %s", (job.publication_name,)).fetchone()
        if not published:
            conn.execute(sql.SQL("CREATE PUBLICATION {} FOR TABLE {}").format(
                sql.Identifier(job.publication_name),
                sql.SQL(", ").join(ident(t) for t in job.all_tables)))


def cleanup_source(admin: Endpoint, job: Job, repl_user: str) -> dict:
    """Undo prepare_source, keeping the marker table as evidence. Safe to run again."""
    repl = sql.Identifier(repl_user)
    with connect(admin) as conn:
        conn.execute("SET default_transaction_read_only = off")
        conn.execute(sql.SQL("DROP PUBLICATION IF EXISTS {}").format(sql.Identifier(job.publication_name)))
        existed = role_exists(conn, repl_user)
        if existed:
            for table in job.all_tables:
                if table_exists(conn, table):
                    conn.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(ident(table), repl))
            for schema in schemas_of(job.all_tables):
                if conn.execute("SELECT to_regnamespace(%s) IS NOT NULL", (schema,)).fetchone()[0]:
                    conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(sql.Identifier(schema), repl))
            conn.execute(sql.SQL("DROP ROLE {}").format(repl))
    return {"publication_dropped": job.publication_name, "role_dropped": existed}


def precheck_source(ep: Endpoint, job: Job) -> list[dict]:
    checks: list[dict] = []
    with connect(ep) as conn:
        version = conn.execute("SHOW server_version_num").fetchone()[0]
        checks.append({"item": "source_version", "ok": int(version) >= 160000, "value": version})
        wal = conn.execute("SHOW wal_level").fetchone()[0]
        checks.append({"item": "source_wal_level", "ok": wal == "logical", "value": wal})
        for table in job.all_tables:
            exists = table_exists(conn, table)
            pk = primary_key(conn, table) if exists else []
            checks.append({"item": f"source_table:{table}", "ok": exists and bool(pk),
                           "value": {"exists": exists, "primary_key": pk}})
        pub = conn.execute(
            "SELECT count(*) FROM pg_publication WHERE pubname = %s", (job.publication_name,)
        ).fetchone()[0]
        # source_prepare creates it; the connector never alters it (autocreate disabled).
        checks.append({"item": "source_publication", "ok": bool(pub),
                       "value": job.publication_name if pub else f"missing: {job.publication_name}"})
    return checks


def precheck_target(ep: Endpoint, job: Job) -> list[dict]:
    checks: list[dict] = []
    with connect(ep) as conn:
        version = conn.execute("SHOW server_version_num").fetchone()[0]
        checks.append({"item": "target_version", "ok": int(version) >= 160000, "value": version})
        for table in job.all_tables:
            if not table_exists(conn, table):
                checks.append({"item": f"target_table:{table}", "ok": True, "value": "absent, will create"})
                continue
            count = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(ident(table))).fetchone()[0]
            # Existing but empty is fine (schema prepared beforehand); data would be overwritten.
            checks.append({"item": f"target_table:{table}", "ok": count == 0, "value": {"rows": count}})
    return checks


def copy_schema(source: Endpoint, target_ddl: Endpoint, target_sink_user: str, job: Job) -> list[str]:
    """Copy table DDL with pg_dump into the target, then grant DML to the sink and app accounts."""
    created: list[str] = []
    with connect(target_ddl) as conn:
        missing = [t for t in job.all_tables if not table_exists(conn, t)]
        for schema in sorted({split(t)[0] for t in job.all_tables}):
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    if missing:
        args = ["pg_dump", "--schema-only", "--no-owner", "--no-privileges"]
        for table in missing:
            args += ["--table", table]
        dump = subprocess.run(args, env=source.env(), capture_output=True, text=True, timeout=60)
        if dump.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {dump.stderr.strip()[:300]}")
        load = subprocess.run(
            ["psql", "--quiet", "--set", "ON_ERROR_STOP=1"],
            input=dump.stdout, env=target_ddl.env(), capture_output=True, text=True, timeout=60,
        )
        if load.returncode != 0:
            raise RuntimeError(f"schema load failed: {load.stderr.strip()[:300]}")
        created = missing
    with connect(target_ddl) as conn:
        for schema in sorted({split(t)[0] for t in job.all_tables}):
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(target_sink_user)))
        for table in job.all_tables:
            conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                ident(table), sql.Identifier(target_sink_user)))
        app_role = job.request.target.app_role
        if app_role:
            for schema in schemas_of(job.request.source.tables):
                conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(app_role)))
            for table in job.request.source.tables:
                conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                    ident(table), sql.Identifier(app_role)))
    return created


def table_digest(conn: psycopg.Connection, table: str) -> dict:
    pk = primary_key(conn, table)
    order = sql.SQL(", ").join(sql.Identifier(c) for c in pk)
    row = conn.execute(
        sql.SQL("SELECT count(*), md5(coalesce(string_agg(md5(t::text), '' ORDER BY {}), '')) FROM {} t")
        .format(order, ident(table))
    ).fetchone()
    return {"rows": row[0], "checksum": row[1]}


def table_rows(conn: psycopg.Connection, table: str) -> int:
    return conn.execute(sql.SQL("SELECT count(*) FROM {}").format(ident(table))).fetchone()[0]


def compare(source: Endpoint, target: Endpoint, tables: list[str]) -> dict:
    """Row count per table on each side, plus a whole-table checksum once the counts agree."""
    result: dict = {"tables": {}, "mismatch": 0}
    with connect(source) as src, connect(target) as tgt:
        for table in tables:
            source_rows = table_rows(src, table)
            target_rows = table_rows(tgt, table) if table_exists(tgt, table) else None
            if source_rows == target_rows:
                s, t = table_digest(src, table), table_digest(tgt, table)
            else:
                s = {"rows": source_rows, "checksum": None}
                t = {"rows": target_rows, "checksum": None}
            same = s == t
            result["tables"][table] = {"source": s, "target": t, "match": same}
            result["mismatch"] += 0 if same else 1
    result["rows_checked"] = sum(v["source"]["rows"] for v in result["tables"].values())
    return result


def copy_done(check: dict, baseline: dict[str, int]) -> bool:
    """Every row the source had when the copy began has reached the target.

    Live writes keep the counts apart, so the goal is the source's count at that moment,
    lowered if rows were deleted since.
    """
    return all((row["target"]["rows"] or 0) >= min(baseline.get(table, 0), row["source"]["rows"])
               for table, row in check["tables"].items())


def _percent(done: int, total: int) -> int:
    # Rounds down, so 100 means every row is there.
    return 100 if total == 0 else min(100, done * 100 // total)


def copy_progress(check: dict) -> dict:
    """Per-table and overall copy progress from a compare() result."""
    tables = {}
    done = total = 0
    for table, row in check["tables"].items():
        source_rows, target_rows = row["source"]["rows"], row["target"]["rows"] or 0
        tables[table] = {"source_rows": source_rows, "target_rows": target_rows,
                         "percent": _percent(target_rows, source_rows), "match": row["match"]}
        done += min(target_rows, source_rows)
        total += source_rows
    return {"tables": tables, "copy_percent": _percent(done, total)}


def slot_lag_bytes(source: Endpoint, slot: str) -> int | None:
    with connect(source) as conn:
        row = conn.execute(
            "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::bigint "
            "FROM pg_replication_slots WHERE slot_name = %s",
            (slot,),
        ).fetchone()
    return None if row is None else int(row[0])


def slot_exists(source: Endpoint, slot: str) -> bool:
    with connect(source) as conn:
        return conn.execute("SELECT 1 FROM pg_replication_slots WHERE slot_name = %s", (slot,)).fetchone() is not None


def drop_slot(source: Endpoint, slot: str) -> bool:
    with connect(source) as conn:
        conn.execute("SET default_transaction_read_only = off")
        row = conn.execute(
            "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots "
            "WHERE slot_name = %s AND NOT active",
            (slot,),
        ).fetchone()
    return row is not None


def block_app_writes(app: Endpoint) -> int:
    """Make the app role read-only for new sessions and end its open sessions.

    A role may change its own session defaults and end its own sessions, so the
    app account needs no extra privilege. Returns the number of sessions ended.
    """
    with connect(app) as conn:
        conn.execute("ALTER ROLE CURRENT_USER SET default_transaction_read_only = on")
        rows = conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename = current_user AND pid <> pg_backend_pid() AND datname = current_database()"
        ).fetchall()
    return len(rows)


def unblock_app_writes(app: Endpoint) -> None:
    with connect(app) as conn:
        # New sessions of the role start read-only, so lift it for this one first.
        conn.execute("SET default_transaction_read_only = off")
        conn.execute("ALTER ROLE CURRENT_USER RESET default_transaction_read_only")
        # Sessions the app opened while blocked keep read-only; end them so it reconnects writable.
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename = current_user AND pid <> pg_backend_pid() AND datname = current_database()"
        )


def insert_marker(source: Endpoint, job_id: str) -> str:
    marker_id = str(uuid.uuid4())
    with connect(source) as conn:
        conn.execute(
            sql.SQL("INSERT INTO {} (id, job_id) VALUES (%s, %s)").format(ident(MARKER_TABLE)),
            (marker_id, job_id),
        )
    return marker_id


def wait_marker(target: Endpoint, marker_id: str, timeout_s: float) -> float:
    """Wait until the marker row reaches the target. Returns seconds waited."""
    started = time.monotonic()
    with connect(target) as conn:
        while True:
            found = conn.execute(
                sql.SQL("SELECT 1 FROM {} WHERE id = %s").format(ident(MARKER_TABLE)), (marker_id,)
            ).fetchone()
            if found:
                return time.monotonic() - started
            if time.monotonic() - started > timeout_s:
                raise TimeoutError(f"marker {marker_id} did not reach target in {timeout_s:.1f}s")
            time.sleep(0.05)


@contextmanager
def job_lock(source: Endpoint, job_id: str) -> Iterator[bool]:
    """Session advisory lock on the source so only one of the 3 agents acts on a job."""
    with connect(source) as conn:
        got = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"alpha_cdc:{job_id}",)).fetchone()[0]
        try:
            yield got
        finally:
            if got:
                conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"alpha_cdc:{job_id}",))
