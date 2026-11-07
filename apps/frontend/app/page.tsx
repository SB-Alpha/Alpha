"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { StateBadge } from "@/components/StateBadge";
import { TARGET_ADAPTERS, adapterFor, GcpTargetAdapter, type TargetInfraType } from "@/lib/adapters/types";
import { STAGE_LABEL, formatTime } from "@/lib/labels";
import { BUTTON_PRIMARY, INPUT } from "@/lib/ui";
import { runTitle, workflows, type MigrationRequest, type RunStatus } from "@/lib/workflows";

const DEFAULT_AWS_REGION = "ap-northeast-2";

type TextField =
  | "service_name"
  | "aws_credentials_ref"
  | "db_admin_ref"
  | "target_project_id"
  | "aws_region"
  | "aws_role_ref"
  | "ecs_service"
  | "rds_instance_id"
  | "db_credentials_ref"
  | "target_region"
  | "target_project_id"
  | "github_repo_url";

type FormState = Record<TextField, string> & { target_preset: TargetInfraType };

type FieldSpec = { key: TextField; label: string; placeholder: string };

const INITIAL: FormState = {
  service_name: "",
  aws_credentials_ref: "",
  db_admin_ref: "",
  target_preset: GcpTargetAdapter.provider,
  target_project_id: "",
  aws_region: DEFAULT_AWS_REGION,
  aws_role_ref: "",
  ecs_service: "",
  rds_instance_id: "",
  db_credentials_ref: "",
  target_region: "",
  github_repo_url: "",
};

const SERVICE_FIELD: FieldSpec = { key: "service_name", label: "서비스 이름", placeholder: "target-app" };
const AWS_CREDENTIALS_FIELD: FieldSpec = {
  key: "aws_credentials_ref",
  label: "원본 AWS 자격증명 참조",
  placeholder: "secret://aws/default",
};
const DB_ADMIN_FIELD: FieldSpec = { key: "db_admin_ref", label: "원본 DB 관리자 참조", placeholder: "secret://db/admin" };

const ADVANCED_FIELDS: FieldSpec[] = [
  { key: "aws_region", label: "원본 AWS 리전", placeholder: DEFAULT_AWS_REGION },
  { key: "aws_role_ref", label: "AWS 조회 역할 ARN", placeholder: "arn:aws:iam::<account>:role/<name>" },
  { key: "ecs_service", label: "ECS 서비스 (비우면 서비스 이름)", placeholder: "target-app" },
  { key: "rds_instance_id", label: "RDS 식별자 (비우면 자동 선택)", placeholder: "target-db" },
  { key: "db_credentials_ref", label: "DB 접속 정보 참조", placeholder: "arn:aws:secretsmanager:..." },
  { key: "github_repo_url", label: "GitHub 저장소 (선택)", placeholder: "https://github.com/org/target-app" },
];

const optional = (value: string) => value.trim() || null;

function toRequest(form: FormState): MigrationRequest {
  const adapter = adapterFor(form.target_preset);
  const serviceName = form.service_name.trim();
  const targetRegion = form.target_region.trim() || adapter.defaultRegion;
  return {
    service_name: serviceName,
    github_repo_url: optional(form.github_repo_url),
    aws_region: form.aws_region.trim() || DEFAULT_AWS_REGION,
    aws_role_ref: optional(form.aws_role_ref),
    aws_credentials_ref: optional(form.aws_credentials_ref),
    ecs_service: optional(form.ecs_service) ?? serviceName,
    // 비우면 assessment 가 계정의 RDS 가 하나뿐일 때 고릅니다. 여러 개면 지정하라고 안내합니다.
    rds_instance_id: optional(form.rds_instance_id),
    db_credentials_ref: optional(form.db_credentials_ref),
    db_admin_ref: optional(form.db_admin_ref),
    target_region: targetRegion,
    target_infra: adapter.toConfig(targetRegion, optional(form.target_project_id)),
  };
}

export default function HomePage() {
  const router = useRouter();
  const [form, setForm] = useState(INITIAL);
  const [runs, setRuns] = useState<RunStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const adapter = adapterFor(form.target_preset);
  const ecsPreview = form.ecs_service.trim() || form.service_name.trim();

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

  const field = ({ key, label, placeholder }: FieldSpec, required: boolean) => (
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
        className={`${INPUT} mt-1 ${key.endsWith("_ref") || key === "target_project_id" ? "font-mono text-xs" : ""}`}
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
          <div>
            {field(SERVICE_FIELD, true)}
            <p className="mt-1 text-xs text-ink/50">
              {ecsPreview ? (
                <>
                  ECS 서비스 <span className="font-mono">{ecsPreview}</span> 를 조사합니다.
                </>
              ) : (
                "이 이름의 ECS 서비스를 조사합니다."
              )}{" "}
              RDS 는 {form.rds_instance_id.trim() ? <span className="font-mono">{form.rds_instance_id.trim()}</span> : "계정에 하나뿐이면 자동으로 고릅니다"}
              {form.rds_instance_id.trim() ? " 를 씁니다." : "."}
            </p>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            {field(AWS_CREDENTIALS_FIELD, true)}
            {field(DB_ADMIN_FIELD, form.db_credentials_ref.trim() === "")}
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label htmlFor="target_preset" className="text-xs font-semibold text-ink/60">
                목적지 클라우드
              </label>
              <select
                id="target_preset"
                value={form.target_preset}
                onChange={(e) =>
                  setForm({
                    ...form,
                    target_preset: e.target.value as TargetInfraType,
                    target_project_id: "",
                  })
                }
                className={`${INPUT} mt-1`}
              >
                {TARGET_ADAPTERS.map((option) => (
                  <option key={option.provider} value={option.provider} disabled={!option.isAvailable}>
                    {option.label}
                    {option.isAvailable ? "" : " · 어댑터 준비 중"}
                  </option>
                ))}
              </select>
            </div>
            <div>
              {field(
                {
                  key: "target_project_id",
                  label: adapter.projectIdLabel,
                  placeholder: adapter.projectIdPlaceholder,
                },
                false,
              )}
              {form.target_preset === "aws" && form.target_project_id.trim().length > 0 && !/^\d{12}$/.test(form.target_project_id.trim()) ? (
                <p className="mt-1 min-h-[1.25rem] text-xs font-medium text-amber-600">
                  ⚠️ AWS 계정 ID는 12자리 숫자여야 합니다 (현재 {form.target_project_id.trim().length}자리).
                </p>
              ) : (
                <p className="mt-1 min-h-[1.25rem] text-xs text-ink/50">{adapter.projectIdHint}</p>
              )}
            </div>
          </div>

          <div>
            <label htmlFor="target_project_id" className="text-xs font-semibold text-ink/60">
              {form.target_preset === "azure"
                ? "목적지 구독 ID (선택)"
                : form.target_preset === "aws"
                ? "목적지 계정 ID (선택)"
                : "목적지 프로젝트 ID (선택)"}
            </label>
            <input
              id="target_project_id"
              value={form.target_project_id}
              onChange={(e) => setForm({ ...form, target_project_id: e.target.value })}
              placeholder={
                form.target_preset === "azure"
                  ? "00000000-0000-0000-0000-000000000000"
                  : form.target_preset === "aws"
                  ? "12자리 숫자 (예: 123456789012)"
                  : "alpha-target-513617"
              }
              className={`${INPUT} mt-1 font-mono text-xs`}
            />
            <p className="mt-1 min-h-[1.25rem] text-xs text-ink/50">
              {form.target_preset === "aws"
                ? "비워둘 시 백엔드 환경변수를 사용하며, 값과 환경변수가 모두 없으면 시작 전 오류('AWS 계정 ID가 없습니다')로 중단됩니다."
                : "비워둘 시 백엔드 기본 환경변수를 사용합니다."}
            </p>
          </div>

          <p className="text-xs text-ink/50">비밀번호·키 원문은 넣지 않습니다. Secret 경로 같은 참조만 넣으세요.</p>

          <details className="rounded-lg border border-ink/10 bg-paper/50 px-4 py-3">
            <summary className="cursor-pointer text-sm font-medium text-ink/70 hover:text-ink">고급 클라우드 설정</summary>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              {ADVANCED_FIELDS.map((f) => field(f, false))}
              {field({ key: "target_region", label: `목적지 ${adapter.cloudName} 리전`, placeholder: adapter.defaultRegion }, false)}
            </div>
          </details>

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
                  <p className="mt-1 truncate text-sm">{runTitle(r.request)}</p>
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
