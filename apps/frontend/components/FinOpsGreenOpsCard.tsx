"use client";

import { useEffect, useState } from "react";
import { PriceApiEstimate, type RealRunTelemetry } from "@/components/finops/PriceApiEstimate";
import { TARGET_ADAPTERS, fetchTargetAvailability, type TargetInfraType } from "@/lib/adapters/types";
import {
  METRIC_KEYS,
  elapsedSeconds,
  formatDuration,
  isFinished,
  numberMetric,
  overallProgress,
  servingSide,
} from "@/lib/dashboard";
import { NOT_PROVIDED, cloudLabel, formatUsd, type TargetProvider } from "@/lib/finops";
import {
  estimateRequest,
  useFinopsEstimate,
  type EstimateInputs,
  type FinopsEstimate,
  type TrafficInputs,
} from "@/lib/finopsApi";
import {
  DEFAULT_PROFILE_ID,
  EMPTY_INPUTS,
  WORKLOAD_PROFILES,
  applyProfile,
  profileById,
  resolveDisk,
  usesExampleValues,
  valueOrigin,
  type FinopsUserInputs,
  type ValueOrigin,
  type WorkloadProfile,
} from "@/lib/finopsProfiles";
import { STAGE_LABEL } from "@/lib/labels";
import { isRecord } from "@/lib/plan";
import type { RunStatus } from "@/lib/workflows";

// 금액·교차점·차익거래는 모두 백엔드 /finops/estimate 가 가격 API 단가로 냅니다. 이 카드는 단가표나 계산식을 갖지 않습니다.
// 입력은 네 가지입니다: run 에서 읽은 값(원본 위치, 계획의 목적지·DB 크기), 예시 프로필 값(표시: 예시값, 가정),
// 사용자가 넣은 값(표시: 사용자 입력), 그리고 모름(미제공). 처음에는 "일반 웹 서비스" 예시 프로필을 넣어 둡니다.

interface Props {
  run: RunStatus;
}

const PRICED_CLOUDS: readonly TargetProvider[] = ["aws", "gcp", "azure"];

const positiveNumber = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;

const isPriced = (cloud: string | null | undefined): cloud is TargetProvider =>
  cloud === "aws" || cloud === "gcp" || cloud === "azure";

/** run 에서 읽는 값. 계획의 DB 크기는 assessment 가 원본 DB 에서 읽은 값입니다 (schemas.TargetInfra). */
function readRunFacts(run: RunStatus) {
  const planning = run.outputs.planning;
  const plan = isRecord(planning) && isRecord(planning.plan) ? planning.plan : null;
  const infra = plan && isRecord(plan.target_infra) ? plan.target_infra : null;
  const kind = run.request.source_kind ?? "aws";
  const sourceCloud = isPriced(kind) ? kind : null;
  const sourceRegion = kind === "aws" ? run.request.aws_region : run.request.cloud_source?.region;
  const targetProvider = run.request.target_infra?.provider ?? "gcp";
  return {
    sourceCloud,
    sourceRegion: sourceRegion?.trim() || null,
    targetProvider: isPriced(targetProvider) ? targetProvider : null,
    targetRegion: run.request.target_infra?.region?.trim() || run.request.target_region?.trim() || null,
    targetAppVcpu: positiveNumber(infra?.cpu),
    targetAppMemoryMib: positiveNumber(infra?.memory),
    dbVcpu: positiveNumber(infra?.db_vcpu),
    dbMemoryGib: positiveNumber(infra?.db_memory_gib),
    dbStorageGb: positiveNumber(infra?.db_disk_size_gb),
  };
}

/** 비교 후보: 백엔드에 어댑터가 등록된 클라우드 중 원본이 아닌 것. 이 run 의 목적지는 run 의 리전, 나머지는 어댑터 기본 리전. */
function useCandidates(facts: ReturnType<typeof readRunFacts>) {
  const [available, setAvailable] = useState<Set<TargetInfraType> | null>(null);
  useEffect(() => {
    fetchTargetAvailability()
      .then(setAvailable)
      .catch(() => setAvailable(new Set()));
  }, []);
  if (available === null) return null;
  return TARGET_ADAPTERS.filter(
    (a) => isPriced(a.provider) && available.has(a.provider) && a.provider !== facts.sourceCloud,
  ).map((a) => ({
    cloud: a.provider,
    region: a.provider === facts.targetProvider && facts.targetRegion ? facts.targetRegion : a.defaultRegion,
  }));
}

type UserInputs = FinopsUserInputs;

const ORIGIN_TEXT: Record<ValueOrigin, string> = {
  missing: NOT_PROVIDED,
  plan: "계획값",
  example: "예시값",
  user: "사용자 입력",
};
const ORIGIN_TONE: Record<ValueOrigin, string> = {
  missing: "text-ink/40",
  plan: "font-semibold text-emerald-800",
  example: "font-semibold text-amber-800",
  user: "font-semibold text-ink",
};

function OriginTag({ origin }: { origin: ValueOrigin }) {
  return <span className={ORIGIN_TONE[origin]}>{ORIGIN_TEXT[origin]}</span>;
}

function NumberField(props: {
  label: string;
  value: number | null;
  origin: ValueOrigin;
  onChange: (v: number | null) => void;
  unit?: string;
  min?: number;
  max?: number;
  step?: number;
}) {
  return (
    <label className="block">
      <span className="flex justify-between font-mono text-[11px] text-ink/65">
        <span>{props.label}</span>
        <OriginTag origin={props.origin} />
      </span>
      <span className="mt-1 flex items-center gap-1">
        <input
          type="number"
          inputMode="decimal"
          value={props.value ?? ""}
          min={props.min}
          max={props.max}
          step={props.step ?? "any"}
          placeholder={NOT_PROVIDED}
          onChange={(e) => {
            const v = e.target.value === "" ? null : Number(e.target.value);
            props.onChange(v === null || Number.isFinite(v) ? v : null);
          }}
          className="w-full rounded border border-ink/30 bg-white px-2 py-0.5 font-mono text-xs"
        />
        {props.unit && <span className="font-mono text-[10px] text-ink/55">{props.unit}</span>}
      </span>
    </label>
  );
}

/** 예시 프로필 고르기. 고르면 아래 값을 그 프로필로 채우고, "비우기" 는 모든 값을 미제공으로 돌립니다. */
function ProfilePicker({
  profile,
  onPick,
}: {
  profile: WorkloadProfile | null;
  onPick: (id: WorkloadProfile["id"] | null) => void;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="예시 프로필">
        <span className="font-mono text-[11px] text-ink/60">예시 프로필</span>
        {WORKLOAD_PROFILES.map((p) => (
          <button
            key={p.id}
            type="button"
            aria-pressed={profile?.id === p.id}
            onClick={() => onPick(p.id)}
            className={`rounded border px-2 py-0.5 text-[11px] transition-colors ${
              profile?.id === p.id
                ? "border-ink bg-ink font-semibold text-white"
                : "border-ink/30 bg-white text-ink/80 hover:border-ink"
            }`}
          >
            {p.label}
          </button>
        ))}
        <button
          type="button"
          onClick={() => onPick(null)}
          className="rounded border border-transparent px-1.5 py-0.5 text-[11px] text-ink/55 underline-offset-2 hover:text-ink hover:underline"
        >
          비우기
        </button>
      </div>
      <p className="text-[11px] text-ink/60">
        {profile
          ? `${profile.label}: ${profile.description}. assessment 가 아직 읽지 못한 값을 이 가정값으로 채웠습니다 (예시값 · 실측 아님). 값을 고치면 "사용자 입력" 으로 계산합니다.`
          : "예시 프로필을 고르지 않았습니다. 비워 둔 값의 금액은 미제공이고, 넣은 값은 \"사용자 입력\" 으로 계산합니다."}
      </p>
    </div>
  );
}

function InputControls({
  inputs,
  setInputs,
  planDiskGb,
  disk,
  profile,
  onPickProfile,
}: {
  inputs: UserInputs;
  setInputs: (next: UserInputs) => void;
  planDiskGb: number | null;
  /** 지금 계산에 쓰는 DB 디스크와 그 출처 (사용자 값 ?? 계획값 ?? 예시값). */
  disk: { value: number | null; origin: ValueOrigin };
  profile: WorkloadProfile | null;
  onPickProfile: (id: WorkloadProfile["id"] | null) => void;
}) {
  const t = inputs.traffic;
  const setTraffic = (patch: Partial<TrafficInputs>) => setInputs({ ...inputs, traffic: { ...t, ...patch } });
  // 프로필 값과 같으면 예시값, 다르면 사용자 입력입니다. DB 디스크는 disk 로 따로 받습니다.
  const ex = profile ? applyProfile(profile) : EMPTY_INPUTS;
  return (
    <div className="mt-3 space-y-3 rounded-lg border border-ink/15 bg-paper/70 px-3.5 py-2.5">
      <ProfilePicker profile={profile} onPick={onPickProfile} />
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <NumberField
          label="원본 앱 vCPU"
          value={inputs.sourceAppVcpu}
          origin={valueOrigin(inputs.sourceAppVcpu, ex.sourceAppVcpu)}
          min={0.25}
          step={0.25}
          onChange={(v) => setInputs({ ...inputs, sourceAppVcpu: v })}
        />
        <NumberField
          label="원본 앱 메모리"
          unit="MiB"
          value={inputs.sourceAppMemoryMib}
          origin={valueOrigin(inputs.sourceAppMemoryMib, ex.sourceAppMemoryMib)}
          min={128}
          step={128}
          onChange={(v) => setInputs({ ...inputs, sourceAppMemoryMib: v === null ? null : Math.round(v) })}
        />
        <label className="block">
          <span className="flex justify-between font-mono text-[11px] text-ink/65">
            <span>원본 RDS 등급</span>
            <OriginTag origin={valueOrigin(inputs.dbInstanceClass, ex.dbInstanceClass)} />
          </span>
          <input
            value={inputs.dbInstanceClass ?? ""}
            placeholder="예: db.t3.micro"
            onChange={(e) => setInputs({ ...inputs, dbInstanceClass: e.target.value.trim() || null })}
            className="mt-1 w-full rounded border border-ink/30 bg-white px-2 py-0.5 font-mono text-xs"
          />
        </label>
        <NumberField
          label={`DB 디스크${planDiskGb !== null && disk.origin === "user" ? ` (계획값 ${planDiskGb}GB)` : ""}`}
          unit="GB"
          value={disk.value}
          origin={disk.origin}
          min={10}
          step={10}
          onChange={(v) => setInputs({ ...inputs, dbStorageGb: v === null ? null : Math.round(v) })}
        />
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <NumberField
          label="요청 있는 시간 RPS"
          value={t.rps}
          origin={valueOrigin(t.rps, ex.traffic.rps)}
          min={0}
          onChange={(v) => setTraffic({ rps: v })}
        />
        <NumberField
          label="유휴 시간"
          unit="%"
          value={t.idlePercent}
          origin={valueOrigin(t.idlePercent, ex.traffic.idlePercent)}
          min={0}
          max={95}
          onChange={(v) => setTraffic({ idlePercent: v })}
        />
        <NumberField
          label="요청당 처리 시간"
          unit="ms"
          value={t.avgLatencyMs}
          origin={valueOrigin(t.avgLatencyMs, ex.traffic.avgLatencyMs)}
          min={1}
          onChange={(v) => setTraffic({ avgLatencyMs: v })}
        />
        <NumberField
          label="읽기 비율"
          unit="%"
          value={t.readRatio === null ? null : Math.round(t.readRatio * 100)}
          origin={valueOrigin(t.readRatio, ex.traffic.readRatio)}
          min={0}
          max={100}
          onChange={(v) => setTraffic({ readRatio: v === null ? null : v / 100 })}
        />
        <NumberField
          label="평균 응답 크기"
          unit="KB"
          value={t.avgResponseKb}
          origin={valueOrigin(t.avgResponseKb, ex.traffic.avgResponseKb)}
          min={0.1}
          onChange={(v) => setTraffic({ avgResponseKb: v })}
        />
      </div>
      <p className="text-[11px] text-ink/55">
        RPS 와 유휴 시간을 둘 다 넣어야 트래픽 조건으로 계산합니다. 차익거래는 원본 앱 크기·처리 시간·읽기 비율·응답 크기가 모두 있을 때만 냅니다.
      </p>
    </div>
  );
}

/** 상단 클라우드 선택: 백엔드 금액이 완전한 클라우드만 순위를 매깁니다. */
function CloudSelector({
  selected,
  onSelect,
  data,
}: {
  selected: TargetProvider;
  onSelect: (p: TargetProvider) => void;
  data: FinopsEstimate | null;
}) {
  const totals = new Map<string, number | null>();
  if (data) {
    totals.set(data.input.current.cloud, data.report.savings[0]?.current_monthly_usd ?? null);
    for (const s of data.report.savings) totals.set(s.cloud, s.complete ? s.candidate_monthly_usd : null);
  }
  const shown = PRICED_CLOUDS.filter((c) => totals.has(c));
  const known = (c: string) => totals.get(c) ?? null;
  const ranked = shown.filter((c) => known(c) !== null).sort((a, b) => (known(a) ?? 0) - (known(b) ?? 0));
  const order = [...ranked, ...shown.filter((c) => !ranked.includes(c))];
  return (
    <div className="inline-flex flex-wrap rounded-lg border border-ink/20 bg-paper p-0.5 font-mono text-xs">
      {order.map((c) => {
        const total = known(c);
        const rank = ranked.indexOf(c);
        return (
          <button
            key={c}
            type="button"
            aria-pressed={selected === c}
            onClick={() => onSelect(c)}
            className={`inline-flex items-center gap-1 rounded px-2.5 py-1 transition-colors ${
              selected === c
                ? "border border-ink bg-white font-bold text-ink shadow-[1px_1px_0_0_var(--color-ink)]"
                : "font-medium text-ink/65 hover:text-ink"
            }`}
          >
            {rank >= 0 && <span className="text-[10px] text-ink/55">{rank + 1}위</span>}
            <span>{cloudLabel(c)}</span>
            {c === data?.input.current.cloud && <span className="text-[10px] text-ink/55">원본</span>}
            <span className="tabular-nums">{total === null ? NOT_PROVIDED : formatUsd(total)}</span>
          </button>
        );
      })}
    </div>
  );
}

function FinOpsCardBody({ run }: Props) {
  const facts = readRunFacts(run);
  const candidates = useCandidates(facts);
  // 처음에는 예시 프로필(일반 웹 서비스)로 채워 FinOps 가 전부 미제공으로 시작하지 않게 합니다. 값은 "예시값" 으로 표시합니다.
  const [profileId, setProfileId] = useState<WorkloadProfile["id"] | null>(DEFAULT_PROFILE_ID);
  const [inputs, setInputs] = useState<UserInputs>(() => {
    const profile = profileById(DEFAULT_PROFILE_ID);
    return profile ? applyProfile(profile) : EMPTY_INPUTS;
  });
  const pickProfile = (id: WorkloadProfile["id"] | null) => {
    const profile = profileById(id);
    setProfileId(profile ? profile.id : null);
    setInputs(profile ? applyProfile(profile) : EMPTY_INPUTS);
  };
  const profile = profileById(profileId);
  // DB 디스크는 상태에 넣지 않고 그릴 때마다 고릅니다. 계획이 나중에 생기면 바로 계획값을 씁니다.
  const disk = resolveDisk(inputs.dbStorageGb, facts.dbStorageGb, profile);
  const exampleBasis =
    profile && (usesExampleValues(inputs, profile) || disk.origin === "example")
      ? `예시 프로필 기준 (가정): ${profile.label}`
      : null;
  const [selected, setSelected] = useState<TargetProvider>(facts.targetProvider ?? "gcp");

  const realLag = numberMetric(run, METRIC_KEYS.replicationLagBytes);
  const estimateInputs: EstimateInputs = {
    sourceCloud: facts.sourceCloud,
    sourceRegion: facts.sourceRegion,
    sourceAppVcpu: inputs.sourceAppVcpu,
    sourceAppMemoryMib: inputs.sourceAppMemoryMib,
    targetAppVcpu: facts.targetAppVcpu,
    targetAppMemoryMib: facts.targetAppMemoryMib,
    dbInstanceClass: inputs.dbInstanceClass,
    dbVcpu: facts.dbVcpu,
    dbMemoryGib: facts.dbMemoryGib,
    dbStorageGb: disk.value,
    candidates: candidates ?? [],
    traffic: inputs.traffic,
    replicationLagBytes: realLag,
  };
  // 후보 목록을 읽기 전에는 요청하지 않습니다 (빈 후보로 한 번 더 부르지 않게)
  const estimate = useFinopsEstimate(candidates === null ? null : estimateRequest(estimateInputs));

  // 실제 run 이 실행 중일 때만 경과 시간을 1초마다 갱신합니다.
  const [nowMs, setNowMs] = useState(() => Date.now());
  const running = !isFinished(run.state);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);

  const realTelemetry: RealRunTelemetry = {
    progress: overallProgress(run),
    elapsedText: formatDuration(elapsedSeconds(run, nowMs)),
    stageLabel: run.current_stage ? STAGE_LABEL[run.current_stage] : isFinished(run.state) ? "종료" : "—",
    replicationLagBytes: realLag,
    servingSide: servingSide(run),
  };

  const setTraffic = (patch: Partial<TrafficInputs>) =>
    setInputs((prev) => ({ ...prev, traffic: { ...prev.traffic, ...patch } }));

  return (
    <section
      aria-labelledby="finops-heading"
      className="rounded-xl border border-ink bg-white p-4 shadow-[3px_3px_0_0_var(--color-ink)]"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-ink/15 pb-3">
        <div className="flex flex-wrap items-baseline gap-2">
          <h2 id="finops-heading" className="text-base font-semibold sm:text-lg">
            멀티클라우드 월 비용 추정
          </h2>
          <span className="rounded border border-ink/30 bg-paper px-1.5 py-0.5 font-mono text-[10px] font-medium text-ink/75">
            가격 API 공시가 · 청구액 아님
          </span>
          {exampleBasis && (
            <span className="rounded border border-amber-400 bg-amber-50 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-amber-900">
              {exampleBasis}
            </span>
          )}
        </div>
        <CloudSelector selected={selected} onSelect={setSelected} data={estimate.status === "ready" ? estimate.data : null} />
      </div>

      {facts.sourceCloud === null || facts.sourceRegion === null ? (
        <p className="mt-3 text-xs text-ink/60">
          원본 클라우드·리전을 run 에서 읽지 못해 비용을 조회하지 않았습니다 ({NOT_PROVIDED}).
        </p>
      ) : (
        <PriceApiEstimate
          state={estimate}
          selectedProvider={selected}
          onSelectProvider={setSelected}
          traffic={inputs.traffic}
          onChangeRps={(rps) => setTraffic({ rps })}
          onChangeIdlePercent={(idlePercent) => setTraffic({ idlePercent })}
          realTelemetry={realTelemetry}
          exampleBasis={exampleBasis}
        />
      )}

      <InputControls
        inputs={inputs}
        setInputs={setInputs}
        planDiskGb={facts.dbStorageGb}
        disk={disk}
        profile={profile}
        onPickProfile={pickProfile}
      />

      {estimate.status === "ready" && estimate.data.input.assumptions.length > 0 && (
        <details className="mt-3 rounded-lg border border-ink/15 bg-paper/30 p-3 text-xs">
          <summary className="cursor-pointer font-semibold text-ink/80 hover:text-ink">계산 가정 (백엔드)</summary>
          <ul className="mt-2 list-disc space-y-0.5 pl-4 text-[11px] text-ink/65">
            {estimate.data.input.assumptions.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

export function FinOpsGreenOpsCard({ run }: Props) {
  return <FinOpsCardBody run={run} />;
}
