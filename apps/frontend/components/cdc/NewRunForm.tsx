"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  CdcApiError,
  CLOUD_LABEL,
  CLOUDS,
  cdcRuns,
  isSingleLine,
  REGION_EXAMPLE,
  REGION_PATTERNS,
  rememberRun,
  type Cloud,
  type CloudStatus,
  type RunInput,
} from "@/lib/cdcRuns";
import { BUTTON_PRIMARY, INPUT } from "@/lib/ui";

interface Side {
  cloud: Cloud;
  region: string;
  user: string;
  password: string;
  database: string;
}
const emptySide = (cloud: Cloud): Side => ({ cloud, region: "", user: "", password: "", database: "" });

// 화면 검사는 백엔드(run_input.py)와 같은 규칙입니다. 최종 판정은 백엔드 422 의 fields 로 표시합니다.
function localProblems(source: Side, target: Side, appUser: string): string[] {
  const problems: string[] = [];
  if (source.cloud === target.cloud) problems.push("target.cloud");
  for (const [name, side] of [["source", source], ["target", target]] as const) {
    if (!REGION_PATTERNS[side.cloud].test(side.region)) problems.push(`${name}.region`);
    if (!isSingleLine(side.user)) problems.push(`${name}.db.user`);
    if (!isSingleLine(side.password)) problems.push(`${name}.db.password`);
    if (side.database !== "" && !isSingleLine(side.database)) problems.push(`${name}.db.database`);
  }
  if (!isSingleLine(appUser)) problems.push("target.db.app_user");
  return problems;
}

const toInput = (source: Side, target: Side, appUser: string): RunInput => ({
  source: {
    cloud: source.cloud,
    region: source.region,
    db: { user: source.user, password: source.password, database: source.database || null },
  },
  target: {
    cloud: target.cloud,
    region: target.region,
    db: { user: target.user, password: target.password, database: target.database || null, app_user: appUser },
  },
});

export function NewRunForm() {
  const router = useRouter();
  const [clouds, setClouds] = useState<CloudStatus[] | null>(null);
  const [source, setSource] = useState<Side>(emptySide("aws"));
  const [target, setTarget] = useState<Side>(emptySide("gcp"));
  const [appUser, setAppUser] = useState("");
  const [touched, setTouched] = useState(false);
  const [serverFields, setServerFields] = useState<string[]>([]);
  const [error, setError] = useState<{ status: number; message: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void cdcRuns
      .listClouds()
      .then(setClouds)
      .catch((e: unknown) => setError({ status: 0, message: e instanceof Error ? e.message : String(e) }));
  }, []);

  const registered = new Set(clouds?.filter((c) => c.registered).map((c) => c.cloud) ?? []);
  const problems = localProblems(source, target, appUser);
  const shown = new Set([...(touched ? problems : []), ...serverFields]);
  const missingKeys = [source.cloud, target.cloud].filter((c) => !registered.has(c));

  const submit = async (e: { preventDefault(): void }) => {
    e.preventDefault();
    setTouched(true);
    setServerFields([]);
    if (problems.length > 0 || missingKeys.length > 0) return;
    setBusy(true);
    setError(null);
    try {
      const { run_id } = await cdcRuns.startRun(toInput(source, target, appUser));
      // 비밀번호는 서버에 저장됐으므로 화면에서 바로 지웁니다.
      setSource((s) => ({ ...s, password: "" }));
      setTarget((t) => ({ ...t, password: "" }));
      rememberRun(run_id, {
        source: { cloud: source.cloud, region: source.region },
        target: { cloud: target.cloud, region: target.region },
      });
      router.push(`/db-migration/runs/${encodeURIComponent(run_id)}`);
    } catch (err) {
      if (err instanceof CdcApiError) {
        setServerFields(err.fields);
        setError({ status: err.status, message: err.message });
      } else {
        setError({ status: 0, message: String(err) });
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-6" noValidate>
      <div className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">새 DB 이전</h1>
        <Link href="/db-migration" className="text-sm text-ink/60 hover:text-ink">
          ← 목록
        </Link>
      </div>
      <p className="text-sm text-ink/60">
        클라우드와 리전만 고르면 저장된 클라우드 키로 PostgreSQL 을 찾습니다. 경로는 AWS·GCP·Azure 중 서로 다른 두 클라우드입니다.
      </p>

      <div className="grid gap-4 md:grid-cols-2">
        <SideFields title="원본" name="source" side={source} onChange={setSource} registered={registered} shown={shown} />
        <SideFields
          title="목적지"
          name="target"
          side={target}
          onChange={setTarget}
          registered={registered}
          shown={shown}
          extra={
            <Field label="앱 계정 (테이블 DML 권한을 받을 사용자)" invalid={shown.has("target.db.app_user")}>
              <input className={INPUT} value={appUser} onChange={(e) => setAppUser(e.target.value)} autoComplete="off" />
            </Field>
          }
        />
      </div>

      {shown.has("target.cloud") && <p className="text-sm text-rose-700">원본과 목적지는 서로 다른 클라우드여야 합니다.</p>}
      {missingKeys.length > 0 && clouds && (
        <p className="text-sm text-amber-800">
          {missingKeys.map((c) => CLOUD_LABEL[c]).join(", ")} 키가 등록되지 않았습니다.{" "}
          <Link href="/db-migration/clouds" className="underline">
            키 등록
          </Link>
        </p>
      )}
      {(shown.has("source.unknown_field") || shown.has("target.unknown_field") || shown.has("unknown_field")) && (
        <p className="text-sm text-rose-700">백엔드가 모르는 입력 필드가 있습니다. 화면과 백엔드 버전을 확인하세요.</p>
      )}
      {error && (
        <p role="alert" className="rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error.status === 409 ? (
            <>
              {error.message} —{" "}
              <Link href="/db-migration/clouds" className="underline">
                클라우드 키 확인
              </Link>
            </>
          ) : (
            error.message
          )}
        </p>
      )}

      <button type="submit" className={BUTTON_PRIMARY} disabled={busy || clouds === null}>
        {busy ? "시작 중…" : "이전 시작"}
      </button>
    </form>
  );
}

function SideFields(props: {
  title: string;
  name: "source" | "target";
  side: Side;
  onChange: (s: Side) => void;
  registered: Set<Cloud>;
  shown: Set<string>;
  extra?: React.ReactNode;
}) {
  const { side, name, shown } = props;
  const set = (patch: Partial<Side>) => props.onChange({ ...side, ...patch });
  return (
    <fieldset className="space-y-3 rounded-lg border border-ink/10 bg-white p-4">
      <legend className="px-1 text-sm font-medium">{props.title}</legend>
      <Field label="클라우드" invalid={false}>
        <select className={INPUT} value={side.cloud} onChange={(e) => set({ cloud: e.target.value as Cloud })}>
          {CLOUDS.map((c) => (
            <option key={c} value={c} disabled={!props.registered.has(c)}>
              {CLOUD_LABEL[c]}
              {props.registered.has(c) ? "" : " (키 없음)"}
            </option>
          ))}
        </select>
      </Field>
      <Field label={`리전 (예: ${REGION_EXAMPLE[side.cloud]})`} invalid={shown.has(`${name}.region`)}>
        <input className={INPUT} value={side.region} onChange={(e) => set({ region: e.target.value })} autoComplete="off" />
      </Field>
      <Field label="DB 관리자 계정" invalid={shown.has(`${name}.db.user`)}>
        <input className={INPUT} value={side.user} onChange={(e) => set({ user: e.target.value })} autoComplete="off" />
      </Field>
      <Field label="DB 관리자 비밀번호" invalid={shown.has(`${name}.db.password`)}>
        <input
          className={INPUT}
          type="password"
          value={side.password}
          onChange={(e) => set({ password: e.target.value })}
          autoComplete="new-password"
        />
      </Field>
      <Field label="DB 이름 (선택: 여러 개면 나중에 고를 수 있음)" invalid={shown.has(`${name}.db.database`)}>
        <input className={INPUT} value={side.database} onChange={(e) => set({ database: e.target.value })} autoComplete="off" />
      </Field>
      {props.extra}
    </fieldset>
  );
}

function Field(props: { label: string; invalid: boolean; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className={`text-xs ${props.invalid ? "text-rose-700" : "text-ink/60"}`}>
        {props.label}
        {props.invalid && " · 확인 필요"}
      </span>
      <div className={props.invalid ? "rounded-md ring-1 ring-rose-400" : ""}>{props.children}</div>
    </label>
  );
}
