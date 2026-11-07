"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { StateBadge } from "@/components/StateBadge";
import {
  TARGET_ADAPTERS,
  adapterFor,
  fetchTargetAvailability,
  GcpTargetAdapter,
  type TargetInfraType,
} from "@/lib/adapters/types";
import { STAGE_LABEL, formatTime } from "@/lib/labels";
import { BUTTON_PRIMARY, INPUT } from "@/lib/ui";
import { runTitle, workflows, type MigrationRequest, type RunStatus, type SourceKind } from "@/lib/workflows";

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
  | "github_repo_url"
  | "src_project_id"
  | "src_subscription_id"
  | "src_resource_group"
  | "src_region"
  | "src_app_name"
  | "src_db_instance"
  | "src_credentials_ref";

type FormState = Record<TextField, string> & { target_preset: TargetInfraType; source_kind: SourceKind };

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
  source_kind: "aws",
  src_project_id: "",
  src_subscription_id: "",
  src_resource_group: "",
  src_region: "",
  src_app_name: "",
  src_db_instance: "",
  src_credentials_ref: "",
};

const SERVICE_FIELD: FieldSpec = { key: "service_name", label: "서비스 이름", placeholder: "target-app" };
const AWS_CREDENTIALS_FIELD: FieldSpec = {
  key: "aws_credentials_ref",
  label: "원본 AWS 자격증명 참조",
  placeholder: "secret://aws/default",
};
const DB_ADMIN_FIELD: FieldSpec = { key: "db_admin_ref", label: "원본 DB 관리자 참조", placeholder: "secret://db/admin" };

const SOURCE_OPTIONS: { kind: SourceKind; label: string }[] = [
  { kind: "aws", label: "AWS (ECS Fargate · RDS)" },
  { kind: "gcp", label: "Google Cloud (Cloud Run · Cloud SQL)" },
  { kind: "azure", label: "Azure (Container Apps · PostgreSQL Flexible)" },
];

// 원본이 GCP·Azure 일 때 입력. 서비스 이름과 별도로 클라우드 쪽 이름(앱·DB 인스턴스)을 받습니다.
const GCP_SOURCE_FIELDS: FieldSpec[] = [
  { key: "src_project_id", label: "원본 GCP 프로젝트 ID", placeholder: "my-project-123" },
  { key: "src_region", label: "원본 리전", placeholder: "asia-northeast3" },
  { key: "src_app_name", label: "Cloud Run 서비스 (비우면 서비스 이름)", placeholder: "target-app" },
  { key: "src_db_instance", label: "Cloud SQL 인스턴스", placeholder: "target-db" },
];
const AZURE_SOURCE_FIELDS: FieldSpec[] = [
  { key: "src_subscription_id", label: "원본 구독 ID", placeholder: "00000000-0000-0000-0000-000000000000" },
  { key: "src_resource_group", label: "리소스 그룹", placeholder: "my-rg" },
  { key: "src_region", label: "원본 위치", placeholder: "koreacentral" },
  { key: "src_app_name", label: "Container App (비우면 서비스 이름)", placeholder: "target-app" },
  { key: "src_db_instance", label: "PostgreSQL Flexible Server", placeholder: "target-db" },
];
const GCP_CREDENTIALS_FIELD: FieldSpec = {
  key: "src_credentials_ref",
  label: "원본 서비스 계정 키 참조 (비우면 서버 로그인)",
  placeholder: "secret://gcp/source",
};
const AZURE_CREDENTIALS_FIELD: FieldSpec = {
  key: "src_credentials_ref",
  label: "원본 서비스 주체 참조",
  placeholder: "secret://azure/source",
};

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
  const common = {
    service_name: serviceName,
    github_repo_url: optional(form.github_repo_url),
    db_admin_ref: optional(form.db_admin_ref),
    target_region: targetRegion,
    target_infra: adapter.toConfig(targetRegion, optional(form.target_project_id)),
  };
  if (form.source_kind !== "aws") {
    // 원본이 GCP·Azure: AWS 조회용 값은 보내지 않고 cloud_source 로 원본 위치를 보냅니다.
    return {
      ...common,
      source_kind: form.source_kind,
      cloud_source: {
        region: form.src_region.trim(),
        app_name: optional(form.src_app_name) ?? serviceName,
        db_instance: form.src_db_instance.trim(),
        credentials_ref: optional(form.src_credentials_ref),
        project_id: form.source_kind === "gcp" ? optional(form.src_project_id) : null,
        subscription_id: form.source_kind === "azure" ? optional(form.src_subscription_id) : null,
        resource_group: form.source_kind === "azure" ? optional(form.src_resource_group) : null,
      },
    };
  }
  return {
    ...common,
    aws_region: form.aws_region.trim() || DEFAULT_AWS_REGION,
    aws_role_ref: optional(form.aws_role_ref),
    aws_credentials_ref: optional(form.aws_credentials_ref),
    ecs_service: optional(form.ecs_service) ?? serviceName,
    // 비우면 assessment 가 계정의 RDS 가 하나뿐일 때 고릅니다. 여러 개면 지정하라고 안내합니다.
    rds_instance_id: optional(form.rds_instance_id),
    db_credentials_ref: optional(form.db_credentials_ref),
  };
}

export default function HomePage() {
  const router = useRouter();
  const [form, setForm] = useState(INITIAL);
  const [runs, setRuns] = useState<RunStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  // 백엔드에 어댑터가 등록된 목적지만 고를 수 있습니다. 읽기 전에는 모두 막습니다.
  const [availableTargets, setAvailableTargets] = useState<Set<TargetInfraType> | null>(null);

  const adapter = adapterFor(form.target_preset);
  const ecsPreview = form.ecs_service.trim() || form.service_name.trim();
  const isAwsSource = form.source_kind === "aws";
  const sourceLabel = form.source_kind === "gcp" ? "Google Cloud" : "Azure";
  const credentialsRefLooksWrong =
    form.src_credentials_ref.trim().length > 0 && !form.src_credentials_ref.trim().startsWith("secret://");

  useEffect(() => {
    workflows
      .list()
      .then(setRuns)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
    fetchTargetAvailability()
      .then(setAvailableTargets)
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
            <label htmlFor="source_kind" className="text-xs font-semibold text-ink/60">
              원본 클라우드
            </label>
            <select
              id="source_kind"
              value={form.source_kind}
              onChange={(e) => setForm({ ...form, source_kind: e.target.value as SourceKind, src_credentials_ref: "" })}
              className={`${INPUT} mt-1`}
            >
              {SOURCE_OPTIONS.map((option) => (
                <option key={option.kind} value={option.kind}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <div>
            {field(SERVICE_FIELD, true)}
            {isAwsSource ? (
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
            ) : (
              <p className="mt-1 text-xs text-ink/50">
                {sourceLabel} 의 앱과 DB 를 클라우드 API 로 읽기만 합니다. 아래에 원본 위치를 적어 주세요.
              </p>
            )}
          </div>

          {isAwsSource ? (
            <div className="grid gap-4 sm:grid-cols-2">
              {field(AWS_CREDENTIALS_FIELD, true)}
              {field(DB_ADMIN_FIELD, form.db_credentials_ref.trim() === "")}
            </div>
          ) : (
            <>
              <div className="grid gap-4 sm:grid-cols-2">
                {(form.source_kind === "gcp" ? GCP_SOURCE_FIELDS : AZURE_SOURCE_FIELDS).map((f) =>
                  field(f, f.key !== "src_app_name"),
                )}
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  {field(
                    form.source_kind === "gcp" ? GCP_CREDENTIALS_FIELD : AZURE_CREDENTIALS_FIELD,
                    form.source_kind === "azure",
                  )}
                  {credentialsRefLooksWrong && (
                    <p className="mt-1 text-xs font-medium text-amber-600">⚠️ secret://… 형식의 참조만 받습니다.</p>
                  )}
                </div>
                {field(DB_ADMIN_FIELD, true)}
              </div>
            </>
          )}

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
                  <option
                    key={option.provider}
                    value={option.provider}
                    disabled={!availableTargets?.has(option.provider)}
                  >
                    {option.label}
                    {availableTargets === null ? " · 확인 중" : availableTargets.has(option.provider) ? "" : " · 어댑터 준비 중"}
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

          <p className="text-xs text-ink/50">비밀번호·키 원문은 넣지 않습니다. Secret 경로 같은 참조만 넣으세요.</p>

          <details className="rounded-lg border border-ink/10 bg-paper/50 px-4 py-3">
            <summary className="cursor-pointer text-sm font-medium text-ink/70 hover:text-ink">고급 클라우드 설정</summary>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              {ADVANCED_FIELDS.filter((f) => isAwsSource || f.key === "github_repo_url").map((f) => field(f, false))}
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
