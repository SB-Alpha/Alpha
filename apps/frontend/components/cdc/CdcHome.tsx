"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  CLOUD_LABEL,
  cdcRuns,
  loadRecentRuns,
  routeText,
  type Cloud,
  type CloudStatus,
  type MigrationRunStatus,
  type RunSummary,
} from "@/lib/cdcRuns";
import { BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SECONDARY, INPUT } from "@/lib/ui";

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** GET /runs 는 진행 중·정리 남은 실행만 줍니다. 이 브라우저의 최근 실행을 뒤에 붙입니다. */
function mergeRuns(active: RunSummary[], recent: RunSummary[]): RunSummary[] {
  const seen = new Set(active.map((r) => r.run_id));
  return [...active, ...recent.filter((r) => !seen.has(r.run_id))];
}

export function CdcHome() {
  const [clouds, setClouds] = useState<CloudStatus[] | null>(null);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void Promise.all([cdcRuns.listClouds(), cdcRuns.listRuns()])
      .then(([c, r]) => {
        setClouds(c);
        setRuns(mergeRuns(r, loadRecentRuns()));
      })
      .catch((e: unknown) => {
        setError(errorText(e));
        setRuns(loadRecentRuns());
      });
  }, []);

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">DB 이전 (CDC)</h1>
        <div className="flex gap-2">
          <Link href="/db-migration/clouds" className={BUTTON_SECONDARY}>
            클라우드 키
          </Link>
          <Link href="/db-migration/new" className={BUTTON_PRIMARY}>
            새 DB 이전
          </Link>
        </div>
      </div>
      {error && (
        <p role="alert" className="rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error}
        </p>
      )}

      <section>
        <h2 className="text-sm font-medium text-ink/70">클라우드 키</h2>
        <ul className="mt-2 grid gap-2 sm:grid-cols-3">
          {(clouds ?? []).map((c) => (
            <li key={c.cloud} className="rounded-lg border border-ink/10 bg-white px-3 py-2 text-sm">
              <span className="font-medium">{CLOUD_LABEL[c.cloud] ?? c.cloud}</span>{" "}
              <span className={c.registered ? "text-emerald-700" : "text-ink/50"}>
                {c.registered ? "등록됨" : "미등록"}
              </span>
            </li>
          ))}
          {clouds === null && !error && <li className="text-sm text-ink/50">불러오는 중…</li>}
        </ul>
      </section>

      <section>
        <h2 className="text-sm font-medium text-ink/70">실행</h2>
        {runs === null ? (
          <p className="mt-2 text-sm text-ink/50">불러오는 중…</p>
        ) : runs.length === 0 ? (
          <p className="mt-2 text-sm text-ink/50">진행 중이거나 정리가 남은 실행이 없습니다.</p>
        ) : (
          <ul className="mt-2 divide-y divide-ink/5 rounded-lg border border-ink/10 bg-white text-sm">
            {runs.map((r) => (
              <li key={r.run_id}>
                <Link
                  href={`/db-migration/runs/${encodeURIComponent(r.run_id)}`}
                  className="flex flex-wrap items-baseline justify-between gap-2 px-3 py-2 hover:bg-paper"
                >
                  <span className="font-mono text-xs">{r.run_id}</span>
                  <span className="text-ink/70">{routeText(r.route)}</span>
                  <span className="text-xs text-ink/50">{r.active ? "진행·정리 중" : "목록 기록"}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------- 클라우드 키 등록

// 백엔드 DELETE 는 남은 VPN 을 아직 검사하지 않아 화면에서 먼저 막습니다 (UI_API.md 와 같은 규칙).
async function fetchKeyState(): Promise<{ clouds: CloudStatus[]; vpnClouds: Set<string> }> {
  const [clouds, runs] = await Promise.all([cdcRuns.listClouds(), cdcRuns.listRuns()]);
  const statuses = await Promise.allSettled(runs.map((r) => cdcRuns.getRun(r.run_id)));
  const vpnClouds = new Set<string>();
  statuses.forEach((s) => {
    if (s.status !== "fulfilled") return;
    const st: MigrationRunStatus = s.value;
    if (st.remaining.includes("vpn") && st.route) {
      vpnClouds.add(st.route.source.cloud);
      vpnClouds.add(st.route.target.cloud);
    }
  });
  return { clouds, vpnClouds };
}

export function CloudKeysPage() {
  const [clouds, setClouds] = useState<CloudStatus[] | null>(null);
  const [vpnClouds, setVpnClouds] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const reload = useCallback(async () => setVersion((v) => v + 1), []);

  useEffect(() => {
    let cancelled = false;
    void fetchKeyState()
      .then((next) => {
        if (cancelled) return;
        setClouds(next.clouds);
        setVpnClouds(next.vpnClouds);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [version]);

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">클라우드 키</h1>
        <Link href="/db-migration" className="text-sm text-ink/60 hover:text-ink">
          ← DB 이전
        </Link>
      </div>
      <p className="text-sm text-ink/60">
        저장한 값은 암호화되어 백엔드에만 남고, 이 화면에는 다시 보이지 않습니다. 같은 클라우드에 다시 저장하면 덮어씁니다.
      </p>
      {error && (
        <p role="alert" className="rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error}
        </p>
      )}
      <div className="grid gap-4 md:grid-cols-3">
        {(clouds ?? []).map((c) => (
          <CloudKeyForm key={c.cloud} status={c} hasVpn={vpnClouds.has(c.cloud)} onSaved={reload} />
        ))}
      </div>
    </div>
  );
}

function CloudKeyForm({ status, hasVpn, onSaved }: { status: CloudStatus; hasVpn: boolean; onSaved: () => Promise<void> }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const cloud = status.cloud as Cloud;
  const complete = status.required_keys.every((k) => (values[k] ?? "").length > 0);

  const save = async () => {
    setBusy(true);
    setNote(null);
    try {
      await cdcRuns.registerCloud(cloud, Object.fromEntries(status.required_keys.map((k) => [k, values[k] ?? ""])));
      setValues({});
      setNote("저장했습니다.");
      await onSaved();
    } catch (e) {
      setNote(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm(`${CLOUD_LABEL[cloud] ?? cloud} 키를 삭제할까요? 되돌릴 수 없습니다.`)) return;
    setBusy(true);
    setNote(null);
    try {
      await cdcRuns.deleteCloud(cloud);
      setNote("삭제했습니다.");
      await onSaved();
    } catch (e) {
      setNote(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const readFile = async (key: string, file: File | undefined) => {
    if (!file) return;
    const text = await file.text();
    setValues((v) => ({ ...v, [key]: text }));
  };

  return (
    <section className="space-y-3 rounded-lg border border-ink/10 bg-white p-4 text-sm">
      <div className="flex items-baseline justify-between">
        <h2 className="font-medium">{CLOUD_LABEL[cloud] ?? cloud}</h2>
        <span className={status.registered ? "text-emerald-700" : "text-ink/50"}>
          {status.registered ? "등록됨" : "미등록"}
        </span>
      </div>
      {status.required_keys.map((key) =>
        key === "credentials_json" ? (
          <label key={key} className="block">
            <span className="text-xs text-ink/60">{key} (서비스 계정 키 JSON 파일)</span>
            <input
              className="mt-1 block w-full text-xs"
              type="file"
              accept="application/json,.json"
              onChange={(e) => void readFile(key, e.target.files?.[0])}
            />
            {values[key] && <span className="text-xs text-ink/50">파일을 읽었습니다 (내용은 표시하지 않음)</span>}
          </label>
        ) : (
          <label key={key} className="block">
            <span className="text-xs text-ink/60">{key}</span>
            <input
              className={INPUT}
              type="password"
              autoComplete="off"
              value={values[key] ?? ""}
              onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.value }))}
            />
          </label>
        ),
      )}
      {note && <p className="text-ink/80">{note}</p>}
      <div className="flex flex-wrap gap-2">
        <button type="button" className={BUTTON_PRIMARY} disabled={busy || !complete} onClick={save}>
          저장
        </button>
        {status.registered && (
          <button
            type="button"
            className={BUTTON_DANGER}
            disabled={busy || hasVpn}
            title={hasVpn ? "VPN 이 남은 실행이 있어 삭제할 수 없습니다" : undefined}
            onClick={remove}
          >
            삭제
          </button>
        )}
      </div>
      {hasVpn && <p className="text-xs text-amber-800">VPN 이 남은 실행이 있어 삭제를 막았습니다. 먼저 정리하세요.</p>}
    </section>
  );
}
