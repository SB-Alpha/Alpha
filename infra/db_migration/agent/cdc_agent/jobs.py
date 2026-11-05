"""Job runner: setup steps (source_prepare → target_prepare → precheck → replication → snapshot → ready) and the cutover.

Setup steps are idempotent and driven by a reconcile loop on every agent. A source
advisory lock makes sure only one agent works on a job at a time, so if the agent
running a job dies, another one continues from the stored phase.
"""

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import psycopg

from cdc_agent import db
from cdc_agent.config import Settings, read_properties
from cdc_agent.connect import ConnectClient, connector_error, connector_running, render
from cdc_agent.models import TERMINAL_STATES, Job, now
from cdc_agent.secret_store import SecretStore
from cdc_agent.store import JobStore
from cdc_agent.worker import WorkerClient

log = logging.getLogger("cdc_agent.jobs")

SETUP_PHASES = {"source_prepare", "target_prepare", "precheck", "replication_setup", "snapshot", "streaming"}
# Connectors keep working here, so a failed check does not end the job.
# Earlier phases fail at once, which reports a wrong password or host.
WATCH_PHASES = {"snapshot", "streaming"}
TEMPORARY_ERRORS = (psycopg.OperationalError, httpx.HTTPError)
ABORT_LOCK_WAIT_S = 20
SLOT_DROP_WAIT_S = 30


class SecretsNotReady(RuntimeError):
    """The secrets file has not reached this node yet. Not a job failure."""


class JobBusy(RuntimeError):
    """Another agent is working on the job. Try again."""


class Credentials:
    def __init__(self, settings: Settings, job: Job):
        path = settings.secrets_dir / f"{job.job_id}.properties"
        if not path.exists():
            raise SecretsNotReady(f"secrets file missing on this node: {path.name}")
        props = read_properties(path)
        src, tgt = job.request.source, job.request.target

        def ep(spec, prefix: str) -> db.Endpoint:
            return db.Endpoint(spec.host, spec.port, spec.database, spec.sslmode,
                               props[f"{prefix}.user"], props[f"{prefix}.password"])

        self.source = ep(src, "source")
        self.source_app = ep(src, "source.app")
        self.source_admin = ep(src, "source.admin")
        self.target = ep(tgt, "target")
        self.target_ddl = ep(tgt, "target.ddl")
        self.target_admin = ep(tgt, "target.admin")


class Runner:
    def __init__(self, settings: Settings, store: JobStore, secrets: SecretStore | None = None):
        self.settings = settings
        self.store = store
        self.secrets = secrets
        self.connect = ConnectClient(settings.connect_url)
        self._stop = threading.Event()


    def start(self) -> None:
        threading.Thread(target=self._loop, name="reconcile", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.settings.reconcile_interval_s):
            try:
                if self.secrets:
                    self.secrets.sync()
            except Exception:  # a sync problem must not stop running jobs
                log.exception("secret sync failed")
            try:
                for job in self.store.all().values():
                    if job.state in ("pending", "running") and job.phase in SETUP_PHASES:
                        self.advance(job.job_id)
                    elif job.state == "waiting_approval" and job.phase == "cutover_ready":
                        self.watch_ready(job.job_id)
            except Exception:  # keep the loop alive; the next tick retries
                log.exception("reconcile tick failed")

    def watch_ready(self, job_id: str) -> None:
        """Report a connector that stopped while waiting for approval.

        No job lock: a cutover needs it the moment it is approved.
        """
        job = self.store.get(job_id)
        try:
            problem = self._connector_problem(job)
        except httpx.HTTPError as exc:
            log.warning("job %s: connector status unavailable: %s", job_id, exc)
            return
        if problem is None:
            return
        job = self.store.get(job_id)  # approved or aborted meanwhile
        if job.state == "waiting_approval" and job.phase == "cutover_ready":
            # streaming: a retry restarts the connectors and checks the data again.
            job.report(state="failed", phase="streaming", progress=50, message=f"동기화 중단: {problem}")
            self.store.put(job)

    def advance(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or job.state in TERMINAL_STATES:
            return
        try:
            creds = Credentials(self.settings, job)
            # The replication account does not exist until source_prepare has run.
            lock_ep = creds.source_admin if job.phase == "source_prepare" else creds.source
            with db.job_lock(lock_ep, job_id) as locked:
                if not locked:
                    return
                job = self.store.get(job_id)  # re-read under the lock
                if job.state in ("pending", "running") and job.phase in SETUP_PHASES:
                    self._step(job, creds)
        except SecretsNotReady as exc:
            log.info("job %s waits: %s", job_id, exc)
        except Exception as exc:
            log.exception("job %s failed in %s", job_id, job.phase)
            # An abort may have finished meanwhile (it drops the replication account).
            latest = self.store.get(job_id)
            if latest is not None and latest.state not in TERMINAL_STATES:
                if isinstance(exc, TEMPORARY_ERRORS) and latest.phase in WATCH_PHASES:
                    # Short VPN or Connect outage: the next tick retries.
                    latest.metrics["measure_error"] = str(exc)
                else:
                    latest.report(state="failed", message=f"{latest.phase}: {exc}")
                self.store.put(latest)

    def _step(self, job: Job, creds: Credentials) -> None:
        if job.phase == "source_prepare":
            job.report(state="running", progress=1, message="원본 준비")
            db.prepare_source(creds.source_admin, job, creds.source.user, creds.source.password)
            job.report(phase="target_prepare", progress=2, message="원본 준비 완료")
            self.store.put(job)

        elif job.phase == "target_prepare":
            job.report(state="running", progress=2, message="목적지 계정 준비")
            db.prepare_target(creds.target_admin, creds.target_ddl, creds.target)
            job.report(phase="precheck", progress=2, message="목적지 계정 준비 완료")
            self.store.put(job)

        elif job.phase == "precheck":
            job.report(state="running", progress=2, message="입력 검사")
            checks = db.precheck_source(creds.source, job) + db.precheck_target(creds.target_ddl, job)
            job.outputs["precheck"] = checks
            failed = [c["item"] for c in checks if not c["ok"]]
            if failed:
                job.report(state="failed", message=f"precheck 실패: {', '.join(failed)}")
            else:
                job.report(phase="replication_setup", progress=10, message="입력 검사 통과")
            self.store.put(job)

        elif job.phase == "replication_setup":
            created = db.copy_schema(creds.source, creds.target_ddl, creds.target.user, job)
            # Topics must exist before the sink subscribes: a regex subscription only
            # notices new topics on its metadata refresh (minutes), too late for the marker.
            self.store.create_topics([f"{job.job_id}.{t}" for t in job.all_tables])
            values = self._template_values(job)
            self.connect.put_config(job.source_connector,
                                    render(self.settings.connectors_dir / "debezium-source.json", values)["config"])
            self.connect.put_config(job.sink_connector,
                                    render(self.settings.connectors_dir / "jdbc-sink.json", values)["config"])
            job.outputs["schema_created"] = created
            job.report(phase="snapshot", progress=20, message="복제 구성 완료, 초기 복사 중")
            self.store.put(job)

        elif job.phase in ("snapshot", "streaming"):
            for name in (job.source_connector, job.sink_connector):
                error = connector_error(self.connect.status(name))
                if error:
                    raise RuntimeError(f"{name}: {error}")
            running = all(connector_running(self.connect.status(n))
                          for n in (job.source_connector, job.sink_connector))
            check = db.compare(creds.source, creds.target, job.request.source.tables)
            job.metrics.update(self._metrics(job, creds, check))
            job.metrics.pop("measure_error", None)
            # Stored so an agent that takes over uses the same goal.
            baseline = job.outputs.setdefault(
                "copy_baseline", {table: row["source"]["rows"] for table, row in check["tables"].items()})
            if running and check["mismatch"] == 0:
                job.evidence["validation"] = {**check, "checked_at": now()}
                job.report(state="waiting_approval", phase="cutover_ready", progress=70,
                           message="동기화 완료, 전환 승인 대기")
            elif job.phase == "snapshot" and running and db.copy_done(check, baseline):
                # Connectors are RUNNING from the start, so wait for the initial copy.
                job.report(phase="streaming", progress=50, message="CDC 동기화 중")
            self.store.put(job)

    def _connector_problem(self, job: Job) -> str | None:
        for name in (job.source_connector, job.sink_connector):
            status = self.connect.status(name)
            if status is None:
                return f"{name}: connector missing"
            error = connector_error(status)
            if error:
                return f"{name}: {error}"
        return None

    def restart_failed_connectors(self, job: Job) -> None:
        if job.phase in WATCH_PHASES:  # earlier phases have no connectors yet
            self.connect.restart_failed(job.source_connector)
            self.connect.restart_failed(job.sink_connector)

    def _template_values(self, job: Job) -> dict[str, str]:
        src, tgt = job.request.source, job.request.target
        return {
            "JOB_ID": job.job_id,
            "SOURCE_HOST": src.host, "SOURCE_PORT": str(src.port), "SOURCE_DB": src.database,
            "SOURCE_SSLMODE": src.sslmode,
            "SLOT_NAME": job.slot_name, "PUBLICATION_NAME": job.publication_name,
            "TABLES": ",".join(job.all_tables),
            "TARGET_HOST": tgt.host, "TARGET_PORT": str(tgt.port), "TARGET_DB": tgt.database,
            "TARGET_SSLMODE": tgt.sslmode,
        }

    def _metrics(self, job: Job, creds: Credentials, check: dict) -> dict:
        return {
            "phase": job.phase,
            "replication_lag_bytes": db.slot_lag_bytes(creds.source, job.slot_name),
            "rows_checked": check["rows_checked"],
            "mismatch": check["mismatch"],
            "measured_at": now(),
            **db.copy_progress(check),
        }

    def refresh_metrics(self, job: Job) -> Job:
        """Live numbers for the dashboard while waiting for approval (not persisted)."""
        if job.state in TERMINAL_STATES or job.phase not in ("streaming", "cutover_ready"):
            return job
        try:
            creds = Credentials(self.settings, job)
            check = db.compare(creds.source, creds.target, job.request.source.tables)
            job.metrics.update(self._metrics(job, creds, check))
            job.metrics.pop("measure_error", None)
        except Exception as exc:
            job.metrics["measure_error"] = str(exc)
        return job


    def start_cutover(self, job: Job, approved_by: str) -> None:
        job.report(state="running", phase="cutover", progress=75, message="쓰기 인계 시작")
        job.evidence["cutover"] = {"approved_by": approved_by, "node": self.settings.node_id, "steps": []}
        self.store.put(job)
        threading.Thread(target=self._cutover, args=(job.job_id,), name=f"cutover-{job.job_id}",
                         daemon=True).start()

    def _cutover(self, job_id: str) -> None:
        job = self.store.get(job_id)
        ev = job.evidence["cutover"]
        started = time.monotonic()

        def step(name: str, **extra) -> None:
            ev["steps"].append({"step": name, "at": now(),
                                "elapsed_ms": round((time.monotonic() - started) * 1000, 1), **extra})

        creds = Credentials(self.settings, job)
        worker = WorkerClient(self.settings)
        blocked = switched = False
        try:
            with db.job_lock(creds.source, job_id) as locked:
                if not locked:
                    raise RuntimeError("another agent holds this job")
                hold = worker.hold(job_id, job.request.options.max_hold_ms)
                t0 = time.monotonic()
                ev["t0"] = hold.get("hold_started_at") or now()
                step("hold")
                status = worker.wait_inflight_zero(self.settings.inflight_timeout_s)
                step("inflight_zero", held_count=status.get("held_count"))
                ended = db.block_app_writes(creds.source_app)
                blocked = True
                step("source_writes_blocked", sessions_ended=ended)
                marker = db.insert_marker(creds.source, job_id)
                budget_s = job.request.options.max_hold_ms / 1000 - (time.monotonic() - t0)
                waited = db.wait_marker(creds.target, marker,
                                        max(0.1, min(self.settings.marker_timeout_s, budget_s)))
                ev["marker_id"] = marker
                step("marker_on_target", waited_ms=round(waited * 1000, 1))
                self.connect.pause(job.sink_connector)
                step("sink_paused")
                # Writes are held and blocked, so both sides are frozen: this is the reference point.
                check = db.compare(creds.source, creds.target, job.request.source.tables)
                job.evidence["validation"] = {**check, "checked_at": now(), "at": "cutover_reference_point"}
                step("validated", mismatch=check["mismatch"])
                if check["mismatch"]:
                    raise RuntimeError(f"data mismatch at reference point: {check['mismatch']} table(s)")
                worker.switch(job_id)
                switched = True
                step("switched_to_target")
                released = worker.release(job_id)
                t1 = time.monotonic()
                ev["t1"] = released.get("released_at") or now()
                ev["hold_ms"] = round((t1 - t0) * 1000, 1)
                ev["held_count"] = released.get("held_count")
                step("released")
            job.metrics.update({"traffic_mode": "TARGET", "mismatch": check["mismatch"],
                                "cutover_ms": ev["hold_ms"]})
            job.report(phase="stabilizing", progress=90,
                       message=f"전환 완료 ({ev['hold_ms']}ms), 안정화 관찰 중")
        except Exception as exc:
            log.exception("cutover %s failed", job_id)
            step("failed", error=str(exc))
            if not switched:
                # No write has reached the target yet: give writes back to the source.
                self._resume_source(job, creds, worker, blocked, step)
                job.report(state="waiting_approval", phase="cutover_ready", progress=70,
                           message=f"전환 실패, 원본으로 재개: {exc}")
            else:
                # Target may already have writes: never go back automatically.
                job.report(state="failed", message=f"목적지로 전환 후 오류, 수동 확인 필요: {exc}")
        self.store.put(job)

    def _resume_source(self, job: Job, creds: Credentials, worker: WorkerClient, blocked: bool, step) -> None:
        # Each step runs on its own: a failed unblock must not leave requests held.
        actions = [("source_writes_unblocked", lambda: db.unblock_app_writes(creds.source_app))] if blocked else []
        actions += [("sink_resumed", lambda: self.connect.resume(job.sink_connector)),
                    ("released_to_source", lambda: worker.release(job.job_id))]
        for name, action in actions:
            try:
                action()
                step(name)
            except Exception as exc:
                step(f"{name}_failed", error=str(exc))

    def close(self, job: Job) -> None:
        creds = Credentials(self.settings, job)
        job.evidence["cleanup"] = self._cleanup(job, creds)
        job.report(state="succeeded", phase="closed", progress=100, message="종료, 복제 자원 정리 완료")
        self.store.put(job)

    def abort(self, job_id: str) -> Job:
        """Same cleanup as close, then record aborted. A failure leaves the job as it is, so call again."""
        if self.secrets:
            self.secrets.sync()
        job = self.store.get(job_id)
        try:
            creds = Credentials(self.settings, job)
        except SecretsNotReady:
            # Secrets never reached the cluster, so nothing was prepared on the databases.
            self.connect.delete(job.source_connector)
            self.connect.delete(job.sink_connector)
            job.evidence["cleanup"] = {"source": "skipped, no secrets", "at": now()}
        else:
            with self._wait_lock(creds.source_admin, job_id):
                job = self.store.get(job_id)  # a step may have finished while waiting
                job.evidence["cleanup"] = self._cleanup(job, creds)
        job.report(state="aborted", message="중단됨, 복제 자원 정리 완료")
        self.store.put(job)
        return job

    @contextmanager
    def _wait_lock(self, ep: db.Endpoint, job_id: str) -> Iterator[None]:
        deadline = time.monotonic() + ABORT_LOCK_WAIT_S
        while True:
            with db.job_lock(ep, job_id) as locked:
                if locked:
                    yield
                    return
            if time.monotonic() > deadline:
                raise JobBusy(f"job {job_id} is busy on another agent")
            time.sleep(1)

    def _cleanup(self, job: Job, creds: Credentials) -> dict:
        admin = creds.source_admin
        self.connect.delete(job.source_connector)
        self.connect.delete(job.sink_connector)
        dropped = self._drop_slot(admin, job.slot_name)
        source = db.cleanup_source(admin, job, creds.source.user)
        if self.secrets:
            self.secrets.delete(job.job_id)
        return {"connectors_deleted": [job.source_connector, job.sink_connector],
                "slot_dropped": dropped, **source, "at": now()}

    def _drop_slot(self, ep: db.Endpoint, slot: str) -> bool:
        # A slot left behind keeps WAL on the source until its disk fills, so wait for it.
        deadline = time.monotonic() + SLOT_DROP_WAIT_S
        while True:
            time.sleep(2)  # let Debezium release the slot before dropping it
            if db.drop_slot(ep, slot):
                return True
            if not db.slot_exists(ep, slot):
                return False
            if time.monotonic() > deadline:
                raise RuntimeError(f"replication slot {slot} is still active")
