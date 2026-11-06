import { recoveryStatus, type RecoveryTone } from "@/lib/cutover";
import type { CheckStatus, ReadinessCheck, RunStatus } from "@/lib/workflows";

const STATUS_TEXT: Record<CheckStatus, { label: string; className: string }> = {
  pass: { label: "통과", className: "text-emerald-700" },
  fail: { label: "실패", className: "text-rose-700" },
  unknown: { label: "미제공", className: "text-amber-700" },
};

const RECOVERY_STYLE: Record<RecoveryTone, string> = {
  safe: "border-emerald-300 bg-emerald-50 text-emerald-950",
  active: "border-sky-300 bg-sky-50 text-sky-950",
  warning: "border-amber-400 bg-amber-50 text-amber-950",
};

export function ReadinessTable({ checks }: { checks: ReadinessCheck[] }) {
  return (
    <table className="mt-3 w-full text-left text-sm">
      <caption className="sr-only">승인 조건 항목</caption>
      <tbody className="divide-y divide-ink/10">
        {checks.map((check) => (
          <tr key={check.id}>
            <th scope="row" className="py-1.5 pr-3 font-medium">
              {check.title}
            </th>
            <td className={`py-1.5 pr-3 font-semibold ${STATUS_TEXT[check.status].className}`}>
              {STATUS_TEXT[check.status].label}
            </td>
            <td className="py-1.5 text-ink/60">{check.detail}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function RecoveryBanner({ run }: { run: RunStatus }) {
  const status = recoveryStatus(run);
  if (!status) return null;
  return (
    <section aria-label="복귀 가능 범위" className={`rounded-lg border p-3 text-sm ${RECOVERY_STYLE[status.tone]}`}>
      <p className="font-semibold">{status.title}</p>
      <p className="mt-0.5">{status.detail}</p>
    </section>
  );
}
