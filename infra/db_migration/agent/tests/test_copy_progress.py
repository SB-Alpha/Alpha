"""Row-count comparison and the copy progress derived from it, without a database."""

from contextlib import contextmanager
from pathlib import Path

import pytest

from cdc_agent import db, jobs
from cdc_agent.config import Settings
from cdc_agent.models import Job, JobRequest

ENDPOINT = db.Endpoint("h", 5432, "d", "require", "u", "p")


@pytest.fixture
def sides(monkeypatch):
    """Row counts per side; records which tables were checksummed."""
    rows = {"source": {}, "target": {}}
    digested = []

    @contextmanager
    def connect(ep):
        yield "source" if ep is SOURCE else "target"

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(db, "table_exists", lambda conn, table: table in rows[conn])
    monkeypatch.setattr(db, "table_rows", lambda conn, table: rows[conn][table])

    def digest(conn, table, deadline=None):
        digested.append((conn, table))
        return {"rows": rows[conn][table], "checksum": rows.get("checksums", {}).get((conn, table), "same")}

    monkeypatch.setattr(db, "table_digest", digest)
    rows["digested"] = digested
    return rows


SOURCE = db.Endpoint("src", 5432, "d", "require", "u", "p")
TARGET = db.Endpoint("tgt", 5432, "d", "require", "u", "p")


def test_tables_under_copy_are_not_checksummed(sides):
    sides["source"]["t"] = 1000
    sides["target"]["t"] = 400

    check = db.compare(SOURCE, TARGET, ["t"])

    assert sides["digested"] == []
    assert check["mismatch"] == 1
    assert check["tables"]["t"]["source"]["rows"] == 1000
    assert check["tables"]["t"]["target"]["rows"] == 400


def test_a_table_not_on_the_target_yet_counts_as_zero_copied(sides):
    sides["source"]["t"] = 10

    check = db.compare(SOURCE, TARGET, ["t"])

    assert check["mismatch"] == 1
    assert db.copy_progress(check)["tables"]["t"] == {
        "source_rows": 10, "target_rows": 0, "percent": 0, "match": False}


def test_equal_counts_are_still_verified_by_checksum(sides):
    sides["source"]["t"] = sides["target"]["t"] = 5
    assert db.compare(SOURCE, TARGET, ["t"])["mismatch"] == 0

    sides["checksums"] = {("target", "t"): "different"}
    check = db.compare(SOURCE, TARGET, ["t"])

    assert check["mismatch"] == 1
    assert db.copy_progress(check)["tables"]["t"]["match"] is False


def _check(**tables):
    return {"tables": {name: {"source": {"rows": s}, "target": {"rows": t}, "match": s == t}
                       for name, (s, t) in tables.items()}}


def test_overall_percent_weighs_tables_by_rows():
    progress = db.copy_progress(_check(big=(900, 450), small=(100, 100)))

    assert progress["copy_percent"] == 55
    assert progress["tables"]["big"]["percent"] == 50
    assert progress["tables"]["small"]["percent"] == 100


def test_extra_target_rows_do_not_hide_a_table_still_copying():
    # After a source delete the target can briefly hold more rows than the source.
    progress = db.copy_progress(_check(a=(100, 150), b=(100, 0)))

    assert progress["tables"]["a"]["percent"] == 100
    assert progress["copy_percent"] == 50


def test_percent_reaches_100_only_when_every_row_is_there():
    assert db.copy_progress(_check(t=(1000, 999)))["copy_percent"] == 99
    assert db.copy_progress(_check(t=(0, 0)))["copy_percent"] == 100


def test_metrics_carry_the_copy_progress(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(jobs.db, "slot_lag_bytes", lambda ep, slot: 7)
    settings = Settings("n1", "http://connect", "kafka", "_jobs", 1, tmp_path, tmp_path,
                        "http://worker", None, 2, 10, 3)
    job = Job(job_id="job01", phase="streaming", request=JobRequest(
        job_id="job01",
        source={"host": "s", "database": "d", "tables": ["public.t"], "app_role": "a"},
        target={"host": "t", "database": "d"}))

    class Creds:
        source = ENDPOINT

    check = {**_check(**{"public.t": (10, 5)}), "rows_checked": 10, "mismatch": 1}
    metrics = jobs.Runner(settings, store=None)._metrics(job, Creds, check)

    assert metrics["copy_percent"] == 50
    assert metrics["tables"]["public.t"]["target_rows"] == 5
    assert metrics["replication_lag_bytes"] == 7
    assert metrics["measured_at"]


def test_compare_without_checksum_only_counts(sides):
    sides["source"]["t"] = sides["target"]["t"] = 5

    check = db.compare(SOURCE, TARGET, ["t"], checksum=False)

    assert sides["digested"] == []
    assert check["mismatch"] == 0
    assert db.copy_progress(check)["copy_percent"] == 100


class FrozenConn(str):
    """A side's name that also records the statements run on it."""

    def __new__(cls, name):
        conn = super().__new__(cls, name)
        conn.statements = []
        return conn

    def execute(self, statement, params=None):
        self.statements.append((statement, params))


def test_frozen_compare_digests_both_sides(sides):
    sides["source"]["t"] = 10
    sides["target"]["t"] = 10
    check = db.compare_frozen(FrozenConn("source"), FrozenConn("target"), ["t"])

    assert sorted(sides["digested"]) == [("source", "t"), ("target", "t")]
    assert (check["mismatch"], check["rows_checked"]) == (0, 10)


def test_frozen_compare_reports_a_different_checksum(sides):
    sides["source"]["t"] = 10
    sides["target"]["t"] = 10
    sides["checksums"] = {("target", "t"): "other"}

    assert db.compare_frozen(FrozenConn("source"), FrozenConn("target"), ["t"])["mismatch"] == 1


def test_frozen_compare_lets_go_of_each_table_before_the_next(sides):
    source, target = FrozenConn("source"), FrozenConn("target")
    for side in ("source", "target"):
        sides[side].update({"a": 1, "b": 2})

    db.compare_frozen(source, target, ["a", "b"])

    # Rolling back to a savepoint drops the locks taken since it.
    for conn in (source, target):
        assert [s for s, _ in conn.statements] == [
            "SAVEPOINT frozen_digest", "ROLLBACK TO SAVEPOINT frozen_digest",
        ] * 2


def test_fixed_snapshot_limits_how_long_it_waits_for_a_lock():
    conn = FrozenConn("source")

    db.fix_snapshot(conn)

    statements = conn.statements
    assert statements[0][0] == "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
    # One statement after BEGIN: it takes the snapshot and sets the limit, so the hold pays one round trip.
    assert len(statements) == 2
    assert "lock_timeout" in statements[1][0] and statements[1][1] == (db.SNAPSHOT_LOCK_TIMEOUT,)
