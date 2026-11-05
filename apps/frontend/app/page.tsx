"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { StateBadge } from "@/components/StateBadge";
import { STAGE_LABEL, formatTime } from "@/lib/labels";
import { BUTTON_PRIMARY, INPUT } from "@/lib/ui";
import { workflows, type MigrationRequest, type RunStatus } from "@/lib/workflows";

type FieldKey = keyof MigrationRequest;

const REQUIRED_FIELDS: { key: FieldKey; label: string; placeholder: string }[] = [
  { key: "github_repo_url", label: "GitHub 저장소", placeholder: "https://github.com/org/target-app" },
  { key: "aws_region", label: "원본 AWS 리전", placeholder: "ap-northeast-2" },
  { key: "target_region", label: "목적지 GCP 리전", placeholder: "asia-northeast3" },
];

const REFERENCE_FIELDS: { key: FieldKey; label: string; placeholder: string }[] = [
  { key: "aws_credentials_ref", label: "AWS 자격증명 참조", placeholder: "secret://aws/default 또는 profile:default" },
  { key: "aws_role_ref", label: "AWS 조회 역할 ARN", placeholder: "arn:aws:iam::<account>:role/<name>" },
  { key: "ecs_service", label: "ECS 서비스", placeholder: "target-app" },
  { key: "rds_instance_id", label: "RDS 식별자", placeholder: "target-db" },
  { key: "db_credentials_ref", label: "DB 접속 정보 참조", placeholder: "arn:aws:secretsmanager:..." },
  { key: "db_admin_ref", label: "DB 관리자 계정 참조", placeholder: "secret://db/admin" },
];

const INITIAL: Record<FieldKey, string> = {
  github_repo_url: "",
  aws_region: "ap-northeast-2",
  target_region: "asia-northeast3",
  aws_role_ref: "",
  aws_credentials_ref: "",
  ecs_service: "",
  rds_instance_id: "",
  db_credentials_ref: "",
  db_admin_ref: "",
};

function toRequest(form: Record<FieldKey, string>): MigrationRequest {
  const optional = (v: string) => (v.trim() === "" ? null : v.trim());
  return {
    github_repo_url: form.github_repo_url.trim(),
    aws_region: form.aws_region.trim(),
    target_region: form.target_region.trim() || "asia-northeast3",
    aws_role_ref: optional(form.aws_role_ref),
    aws_credentials_ref: optional(form.aws_credentials_ref),
    ecs_service: optional(form.ecs_service),
    rds_instance_id: optional(form.rds_instance_id),
    db_credentials_ref: optional(form.db_credentials_ref),
    db_admin_ref: optional(form.db_admin_ref),
  };
}

export default function HomePage() {
  const router = useRouter();
  const [form, setForm] = useState(INITIAL);
  const [runs, setRuns] = useState<RunStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    workflows
      .list()
      .then(setRuns)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);
    try {
      const run = await workflows.create(toRequest(form));
      router.push(`/runs/${run.run_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setIsSubmitting(false);
    }
  };

  const field = ({ key, label, placeholder }: { key: FieldKey; label: string; placeholder: string }, required: boolean) => (
    <div key={key}>
      <label htmlFor={key} className="text-xs font-semibold text-ink/60">
        {label}
        {required && <span className="text-rose-600"> *</span>}
      </label>
      <input
        id={key}
        required={required}
        value={form[key]}
        onChange={(e) => setForm({ ...form, [key]: e.target.value })}
        placeholder={placeholder}
        className={`${INPUT} mt-1 ${key.endsWith("_ref") ? "font-mono text-xs" : ""}`}
      />
    </div>
  );

  return (
    <div className="grid gap-10 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <section aria-labelledby="new-run">
        <p className="font-mono text-xs text-ink/50">STEP 0 · 이전 요청</p>
        <h1 id="new-run" className="mt-2 text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">
          무엇을 옮길까요?
        </h1>
        <p className="mt-3 max-w-prose text-ink/70">
          원본 서비스를 알려 주면 조사 → 계획 → <b>승인 ①</b> → 목적지 구축 → DB 동기화 → <b>승인 ②</b> → 전환 순서로
          진행합니다. 사람 승인이 필요한 두 지점에서 멈춥니다.
        </p>

        <form onSubmit={handleSubmit} className="mt-6 space-y-5 rounded-xl border border-ink/15 bg-white p-5">
          <div className="grid gap-4 sm:grid-cols-2">
            {REQUIRED_FIELDS.map((f, i) => (
              <div key={f.key} className={i === 0 ? "sm:col-span-2" : ""}>
                {field(f, f.key !== "target_region")}
              </div>
            ))}
          </div>
          <fieldset className="border-t border-ink/10 pt-4">
            <legend className="pr-2 text-xs font-semibold text-ink/60">접근 참조 (선택)</legend>
            <p className="mb-3 text-xs text-ink/50">비밀번호·키 원문은 넣지 않습니다. ARN·Secret 경로 같은 참조만 넣으세요.</p>
            <div className="grid gap-4 sm:grid-cols-2">{REFERENCE_FIELDS.map((f) => field(f, false))}</div>
          </fieldset>
          {error && (
            <p role="alert" className="rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-800">
              {error}
            </p>
          )}
          <button type="submit" disabled={isSubmitting} className={`${BUTTON_PRIMARY} w-full sm:w-auto`}>
            {isSubmitting ? "만드는 중…" : "이전 시작"}
          </button>
        </form>
      </section>

      <section aria-labelledby="recent-runs">
        <h2 id="recent-runs" className="text-sm font-semibold text-ink/60">
          최근 이전
        </h2>
        {runs === null ? (
          <p className="mt-3 text-sm text-ink/40">{error ? "목록을 불러오지 못했습니다" : "불러오는 중…"}</p>
        ) : runs.length === 0 ? (
          <p className="mt-3 text-sm text-ink/40">아직 없습니다</p>
        ) : (
          <ul className="mt-3 space-y-2">
            {runs.map((r) => (
              <li key={r.run_id}>
                <Link
                  href={`/runs/${r.run_id}`}
                  className="block rounded-lg border border-ink/15 bg-white px-4 py-3 transition-colors hover:border-ink"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-xs">{r.run_id}</span>
                    <StateBadge state={r.state} />
                  </div>
                  <p className="mt-1 truncate text-sm">{r.request.github_repo_url}</p>
                  <p className="mt-0.5 text-xs text-ink/50">
                    {r.current_stage ? STAGE_LABEL[r.current_stage] : "끝"} · {formatTime(r.created_at)}
                  </p>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
