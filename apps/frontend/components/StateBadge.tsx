import { STATE_LABEL, STATE_TONE } from "@/lib/labels";
import type { WorkflowState } from "@/lib/workflows";

type Props = { state: WorkflowState };

export function StateBadge({ state }: Props) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${STATE_TONE[state]}`}
    >
      {STATE_LABEL[state]}
    </span>
  );
}
