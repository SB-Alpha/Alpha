import { recoveryStatus, type RecoveryTone } from "@/lib/cutover";
import type { RunStatus } from "@/lib/workflows";

const RECOVERY_STYLE: Record<RecoveryTone, string> = {
  safe: "border-emerald-300 bg-emerald-50 text-emerald-950",
  active: "border-sky-300 bg-sky-50 text-sky-950",
  warning: "border-amber-400 bg-amber-50 text-amber-950",
};

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
