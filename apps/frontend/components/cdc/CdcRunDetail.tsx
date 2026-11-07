"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  CdcApiError,
  cdcRuns,
  cleanupAction,
  errorGuide,
  isSettled,
  PHASE_LABEL,
  progressPercent,
  rememberRun,
  remainingLabel,
  routeText,
  STATE_LABEL,
  type FinishResult,
  type MigrationRunStatus,
  type RunMetrics,
} from "@/lib/cdcRuns";
import { formatBytes } from "@/lib/dashboard";
import { BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SECONDARY, INPUT } from "@/lib/ui";

// 에이전트 조회는 VPN 터널을 지나므로 4초보다 자주 부르지 않습니다.
const POLL_MS = 4000;
const NA = "미제공";
// step 은 전환부터 null 입니다. 상태를 모를 때도 null 이라 단계 이름으로 나눕니다.
const POST_CUTOVER_PHASES = new Set(["cutover", "stabilizing", "closed"]);

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function CdcRunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const [status, setStatus] = useState<MigrationRunStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // GET /runs 에 있는 실행만 이 앱이 시작·정리할 수 있습니다. workflows 가 시작한 실행은 읽기 전용입니다.
  const [isOwned, setIsOwned] = useState<boolean | null>(null);
  const inFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const next = await cdcRuns.getRun(runId);
      setStatus(next);
      setLoadError(null);
      rememberRun(next.run_id, next.route);
    } catch (e) {
      setLoadError(errorText(e));
    } finally {
      inFlight.current = false;
    }
  }, [runId]);

  useEffect(() => {
    void cdcRuns
      .listRuns()
      .then((runs) => setIsOwned(runs.some((r) => r.run_id === runId)))
      .catch(() => setIsOwned(false));
  }, [runId]);

  const settled = status !== null && isSettled(status);
  useEffect(() => {
    if (settled) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      await refresh();
      if (!cancelled) timer = setTimeout(tick, POLL_MS);
    };
    void tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [refresh, settled]);

  return (
    <div className="space-y-6">
      <Link href="/db-migration" className="text-sm text-ink/60 hover:text-ink">
        ← DB 이전 목록
      </Link>

      {loadError && (
        <p role="alert" className="rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {loadError}
        </p>
      )}

      {status === null ? (
        !loadError && <p className="text-ink/50">불러오는 중…</p>
      ) : (
        <>
          <RunHeader status={status} />
          {status.metrics.simulated === true && (
            <p role="alert" className="rounded-md border border-rose-400 bg-rose-50 px-3 py-2 text-sm font-medium text-rose-800">
              모의 CDC 에이전트의 값입니다. 실제 복제가 아닙니다 (ALPHA_CDC_SIMULATION).
            </p>
          )}
          {isOwned === false && (
            <p className="rounded-md border border-ink/15 bg-white px-3 py-2 text-sm text-ink/70">
              이 앱의 실행 목록(GET /db-migration/runs)에 없는 실행입니다. 상태만 보여 주며 재실행·전환·정리 버튼은 열지 않습니다.
            </p>
          )}
          <ProgressCard status={status} />
          <ErrorCard status={status} />
          {isOwned && status.database_choices && <DatabaseChoicePicker status={status} onDone={refresh} />}
          {isOwned && <RetryCard status={status} onDone={refresh} />}
          {isOwned && status.ready_for_cutover && <CutoverApproval status={status} onDone={refresh} />}
          <MetricsGrid metrics={status.metrics} />
          {isOwned && <CleanupPanel status={status} onDone={refresh} />}
        </>
      )}
    </div>
  );
}

function RunHeader({ status }: { status: MigrationRunStatus }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <p className="font-mono text-xs text-ink/50">
          {status.run_id} · job {status.job_id}
        </p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">{routeText(status.route)}</h1>
        <p className="mt-1 text-sm text-ink/60">
          {PHASE_LABEL[status.phase] ?? status.phase}
          {status.step !== null
            ? ` · ${status.step}/${status.last_step} 단계`
            : POST_CUTOVER_PHASES.has(status.phase)
              ? " · 전환 이후"
              : ""}
        </p>
      </div>
      <div className="text-right">
        <span className="rounded-full border border-ink/20 bg-white px-3 py-1 text-sm font-medium">
          {STATE_LABEL[status.state] ?? status.state}
        </span>
        {status.message && <p className="mt-1 max-w-sm text-sm text-ink/70">{status.message}</p>}
      </div>
    </header>
  );
}

function ProgressCard({ status }: { status: MigrationRunStatus }) {
  const progress = progressPercent(status);
  return (
    <section className="rounded-lg border border-ink/10 bg-white p-4">
      <div className="flex items-baseline justify-between text-sm">
        <h2 className="font-medium">진행</h2>
        <span className="font-mono text-ink/70">
          {progress === null ? NA : `${progress.value.toFixed(progress.basis === "copy" ? 1 : 0)}%`}
        </span>
      </div>
      <div className="mt-2 h-2 overflow-hidden rounded-full bg-ink/10" aria-hidden>
        {progress && <div className="h-full bg-accent transition-[width]" style={{ width: `${progress.value}%` }} />}
      </div>
      <p className="mt-2 text-xs text-ink/50">
        {progress === null
          ? "단계와 복사율이 아직 없습니다."
          : progress.basis === "copy"
            ? "초기 복사율 (에이전트 실측)"
            : "작업 등록 전 단계 진행률 (step / last_step)"}
      </p>
    </section>
  );
}

function ErrorCard({ status }: { status: MigrationRunStatus }) {
  if (!status.error) return null;
  const guide = errorGuide(status.error.code);
  return (
    <section role="alert" className="rounded-lg border border-rose-300 bg-rose-50 p-4 text-sm text-rose-900">
      <p className="font-mono text-xs text-rose-700">{status.error.code}</p>
      {guide && <p className="mt-1 font-medium">{guide}</p>}
      <p className="mt-1 whitespace-pre-wrap">{status.error.message}</p>
    </section>
  );
}

function DatabaseChoicePicker({ status, onDone }: { status: MigrationRunStatus; onDone: () => Promise<void> }) {
  const choices = status.database_choices!;
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const needSource = choices.source.length > 0;
  const needTarget = choices.target.length > 0;
  const ready = (!needSource || source) && (!needTarget || target);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await cdcRuns.retry(status.run_id, {
        ...(needSource ? { source_database: source } : {}),
        ...(needTarget ? { target_database: target } : {}),
      });
      await onDone();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm">
      <h2 className="font-medium">DB 선택</h2>
      <p className="mt-1 text-ink/70">서버에 DB 가 여러 개입니다. 이전할 DB 를 고르면 저장된 입력으로 다시 실행합니다.</p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        {needSource && <Choice label="원본 DB" options={choices.source} value={source} onChange={setSource} />}
        {needTarget && <Choice label="목적지 DB" options={choices.target} value={target} onChange={setTarget} />}
      </div>
      {error && <p className="mt-2 text-rose-700">{error}</p>}
      <button type="button" className={`${BUTTON_PRIMARY} mt-3`} disabled={!ready || busy} onClick={submit}>
        {busy ? "요청 중…" : "고른 DB 로 다시 실행"}
      </button>
    </section>
  );
}

function Choice(props: { label: string; options: string[]; value: string; onChange: (v: string) => void }) {
  return (
    <label className="block">
      <span className="text-xs text-ink/60">{props.label}</span>
      <select className={INPUT} value={props.value} onChange={(e) => props.onChange(e.target.value)}>
        <option value="">선택</option>
        {props.options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

// 실패한 실행은 저장된 입력으로 다시 시작합니다. 비밀번호를 다시 받지 않습니다.
function RetryCard({ status, onDone }: { status: MigrationRunStatus; onDone: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (status.state !== "failed" || status.database_choices) return null;
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await cdcRuns.retry(status.run_id);
      await onDone();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="flex flex-wrap items-center gap-3 rounded-lg border border-ink/10 bg-white p-4 text-sm">
      <button type="button" className={BUTTON_SECONDARY} disabled={busy} onClick={submit}>
        {busy ? "요청 중…" : "저장된 입력으로 다시 실행"}
      </button>
      {error && <span className="text-rose-700">{error}</span>}
    </section>
  );
}

function CutoverApproval({ status, onDone }: { status: MigrationRunStatus; onDone: () => Promise<void> }) {
  const [approver, setApprover] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setNote(null);
    try {
      const result = await cdcRuns.cutover(status.run_id, approver.trim());
      setNote(
        result.outcome === "unknown"
          ? `전환 결과를 확정하지 못했습니다 (${result.message}). Worker 의 쓰기 위치를 확인하세요.`
          : result.message,
      );
    } catch (e) {
      // 시간 초과는 전환이 서버에서 계속 진행 중일 수 있으므로 다시 보내지 않고 상태로 확인합니다.
      setNote(
        e instanceof CdcApiError && e.isTransport
          ? "응답을 받지 못했습니다. 다시 누르지 말고 아래 전환 결과를 확인하세요."
          : errorText(e),
      );
    } finally {
      setBusy(false);
      setConfirmed(false);
      await onDone();
    }
  };

  return (
    <section className="rounded-lg border border-accent/40 bg-white p-4 text-sm">
      <h2 className="font-medium">쓰기 전환 승인</h2>
      <p className="mt-1 text-ink/70">복제가 따라잡았습니다. 승인하면 원본 쓰기를 막고 목적지로 전환합니다 (최대 60초).</p>
      <div className="mt-3 flex flex-wrap items-end gap-3">
        <label className="block min-w-48 flex-1">
          <span className="text-xs text-ink/60">승인자</span>
          <input className={INPUT} value={approver} onChange={(e) => setApprover(e.target.value)} placeholder="이름" />
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
          <span>원본 쓰기 차단을 확인했습니다</span>
        </label>
        <button
          type="button"
          className={BUTTON_PRIMARY}
          disabled={busy || !confirmed || approver.trim().length === 0}
          onClick={submit}
        >
          {busy ? "전환 중…" : "전환 승인"}
        </button>
      </div>
      {note && <p className="mt-2 text-ink/80">{note}</p>}
    </section>
  );
}

function CleanupPanel({ status, onDone }: { status: MigrationRunStatus; onDone: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<FinishResult | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const action = cleanupAction(status);
  const hasLeftovers = status.cleanup_required || status.remaining.length > 0;
  const canStop = action === "stop" && status.metrics.serving !== "target";
  if (!hasLeftovers && result === null) return null;

  const submit = async () => {
    setBusy(true);
    setNote(null);
    try {
      const next = action === "finish" ? await cdcRuns.finish(status.run_id) : await cdcRuns.stop(status.run_id);
      setResult(next);
    } catch (e) {
      // 정리는 수십 분 걸립니다. 시간 초과·409 는 서버에서 정리 중이라는 뜻이라 상태를 계속 봅니다.
      if (e instanceof CdcApiError && (e.isTransport || e.status === 409)) {
        setNote("정리가 서버에서 계속 진행 중입니다. 남은 항목이 줄어드는지 확인하세요.");
      } else {
        setNote(errorText(e));
      }
    } finally {
      setBusy(false);
      await onDone();
    }
  };

  const leftovers = result?.remaining ?? status.remaining;
  return (
    <section className="rounded-lg border border-ink/10 bg-white p-4 text-sm">
      <h2 className="font-medium">정리</h2>
      {leftovers.length > 0 ? (
        <ul className="mt-2 list-disc pl-5 text-ink/80">
          {leftovers.map((item) => (
            <li key={item}>{remainingLabel(item)}</li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-ink/60">남은 항목이 없습니다.</p>
      )}
      {result && result.next_steps.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-ink/70">
          {result.next_steps.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      )}
      {result && (
        <p className="mt-2 text-ink/70">
          {result.state === "finished" ? "정리를 마쳤습니다." : "일부가 남았습니다. 같은 정리를 다시 실행하세요."} VPN:{" "}
          {result.vpn === "removed" ? "삭제됨" : "남아 있음"}
        </p>
      )}
      {note && <p className="mt-2 text-ink/80">{note}</p>}
      <div className="mt-3">
        {action === "finish" ? (
          <button type="button" className={BUTTON_PRIMARY} disabled={busy} onClick={submit}>
            {busy ? "정리 중…" : result?.state === "incomplete" ? "다시 정리" : "전환 후 정리 (finish)"}
          </button>
        ) : (
          canStop && (
            <button type="button" className={BUTTON_DANGER} disabled={busy} onClick={submit}>
              {busy ? "정리 중…" : result?.state === "incomplete" ? "다시 정리" : "전환 전 중단·정리 (stop)"}
            </button>
          )
        )}
      </div>
    </section>
  );
}

function MetricsGrid({ metrics }: { metrics: RunMetrics }) {
  const rows: [string, string][] = [
    ["초기 복사율", metrics.copy_percent === undefined ? NA : `${metrics.copy_percent.toFixed(1)}%`],
    ["복제 지연", metrics.replication_lag_bytes === undefined ? NA : formatBytes(metrics.replication_lag_bytes)],
    ["비교한 행", metrics.rows_checked === undefined ? NA : metrics.rows_checked.toLocaleString("ko-KR")],
    ["불일치 행", metrics.mismatch === undefined ? NA : metrics.mismatch.toLocaleString("ko-KR")],
    ["쓰기 위치", metrics.serving === undefined ? NA : metrics.serving === "target" ? "목적지" : "원본"],
    ["목적지 DB 네트워크", metrics.target_db_network ?? NA],
    ["전환 결과", metrics.cutover_outcome ?? NA],
    [
      "전환 시간",
      metrics.cutover_seconds === undefined
        ? NA
        : `${metrics.cutover_seconds.toFixed(2)}초 (${metrics.cutover_seconds_source ?? NA})`,
    ],
    ["전환 순서", metrics.cutover_sequence ?? NA],
    ["검증", metrics.verification ?? NA],
    ["측정 시각", metrics.measured_at ?? NA],
  ];
  const tables = metrics.tables ? Object.entries(metrics.tables) : [];
  return (
    <section className="rounded-lg border border-ink/10 bg-white p-4 text-sm">
      <h2 className="font-medium">지표</h2>
      {metrics.measure_error && (
        <p className="mt-2 rounded border border-amber-300 bg-amber-50 px-2 py-1 text-amber-900">
          지표 측정 실패: 아래 값이 오래됐을 수 있습니다 ({metrics.measure_error})
        </p>
      )}
      <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
        {rows.map(([k, v]) => (
          <div key={k} className="flex justify-between gap-3 border-b border-ink/5 pb-1">
            <dt className="text-ink/60">{k}</dt>
            <dd className={`font-mono ${v === NA ? "text-ink/40" : ""}`}>{v}</dd>
          </div>
        ))}
      </dl>
      {tables.length > 0 && (
        <table className="mt-4 w-full text-left text-xs">
          <thead className="text-ink/50">
            <tr>
              <th className="py-1">테이블</th>
              <th>원본 행</th>
              <th>목적지 행</th>
              <th>복사율</th>
              <th>일치</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {tables.map(([name, t]) => (
              <tr key={name} className="border-t border-ink/5">
                <td className="py-1">{name}</td>
                <td>{t.source_rows.toLocaleString("ko-KR")}</td>
                <td>{t.target_rows.toLocaleString("ko-KR")}</td>
                <td>{t.percent === undefined ? NA : `${t.percent.toFixed(1)}%`}</td>
                <td>{t.match === undefined ? NA : t.match ? "일치" : "불일치"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {metrics.cutover_steps && metrics.cutover_steps.length > 0 && (
        <ol className="mt-4 space-y-1 text-xs">
          {metrics.cutover_steps.map((s, i) => (
            <li key={`${s.step}-${i}`} className="flex justify-between font-mono">
              <span>{s.step}</span>
              <span className={s.error ? "text-rose-700" : "text-ink/60"}>
                {s.error ?? (s.elapsed_ms === undefined ? NA : `${s.elapsed_ms} ms`)}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
