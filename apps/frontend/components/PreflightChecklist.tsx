import { preflightVerdict } from "@/lib/dashboard";
import { PREFLIGHT_VERDICT_LABEL, formatTime } from "@/lib/labels";
import type { PreflightCheck, PreflightReport, PreflightStatus, PreflightVerdict } from "@/lib/workflows";

type Props = {
  report: PreflightReport | null;
  isLoading: boolean;
  error: string | null;
};

type StatusMeta = { label: string; tone: string };

// 색만으로 구분하지 않도록 모든 상태에 글자 표시를 둡니다.
const STATUS_META: Record<PreflightStatus, StatusMeta> = {
  pass: { label: "통과", tone: "text-emerald-700" },
  fail: { label: "실패", tone: "text-rose-700" },
  unknown: { label: "미확인", tone: "text-amber-700" },
  info: { label: "참고", tone: "text-ink/60" },
};

const VERDICT_TONE: Record<PreflightVerdict, string> = {
  go: "border-emerald-300 bg-emerald-50 text-emerald-900",
  no_go: "border-rose-300 bg-rose-50 text-rose-800",
  unverified: "border-amber-400 bg-amber-50 text-amber-900",
};

function statusMeta(check: PreflightCheck): StatusMeta {
  return STATUS_META[check.status] ?? STATUS_META.unknown;
}

function formatValue(value: number | string): string {
  return typeof value === "number" ? value.toLocaleString("ko-KR") : value;
}

export function PreflightChecklist({ report, isLoading, error }: Props) {
  if (isLoading) {
    return (
      <p role="status" className="rounded-md border border-ink/15 bg-stone-50 px-3 py-2 text-sm text-ink/70">
        <span className="inline-block motion-safe:animate-pulse">원본과 목적지 DB를 비교해 점검하는 중… 결과가 아직 없습니다.</span>
      </p>
    );
  }

  if (report === null) {
    return (
      <p role="alert" className="rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800">
        <span className="font-semibold">점검 결과 없음.</span> 불러오지 못했습니다{error ? `: ${error}` : "."}
      </p>
    );
  }

  const verdict = preflightVerdict(report);

  return (
    <div className="space-y-2">
      <p role="status" className={`rounded-md border px-3 py-2 text-sm font-semibold ${VERDICT_TONE[verdict]}`}>
        판정: {PREFLIGHT_VERDICT_LABEL[verdict]}
      </p>

      {report.checks.length === 0 ? (
        <p className="text-sm text-ink/50">점검 항목 미제공</p>
      ) : (
        <ul className="divide-y divide-ink/10">
          {report.checks.map((check) => {
            const meta = statusMeta(check);
            return (
              <li key={check.id} className="py-2">
                <div className="flex justify-between gap-3 text-sm">
                  <span className="min-w-0 break-words">{check.label}</span>
                  <span className={`shrink-0 font-medium ${meta.tone}`}>{meta.label}</span>
                </div>
                {check.value != null && (
                  <p className="mt-0.5 break-all font-mono text-xs tabular-nums text-ink/70">{formatValue(check.value)}</p>
                )}
                {check.detail && <p className="mt-0.5 break-words text-xs text-ink/55">{check.detail}</p>}
              </li>
            );
          })}
        </ul>
      )}

      <p className="text-xs text-ink/50">측정 {formatTime(report.measured_at)}</p>
    </div>
  );
}
