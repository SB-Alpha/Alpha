import { NOT_PROVIDED, basisSummaryLine, type BothBases } from "@/lib/finops";

interface Props {
  bases: BothBases;
  planDbTier: string | null;
}

/** DB 기준별 증감률. 수치와 조건을 한 문단에 묶어 조건 없이 읽히지 않게 합니다. */
export function DbBasisRows({ bases, planDbTier }: Props) {
  const { sameMemory, asDeployed } = bases;
  return (
    <div className="mt-4 rounded-lg border border-ink/15 bg-paper/40 p-3 text-xs leading-relaxed text-ink/80">
      <h3 className="font-semibold text-ink">DB 등급 기준별 차이 (AWS 대비)</h3>
      <p className="mt-1.5">{basisSummaryLine(sameMemory)}</p>
      <p className="mt-1.5">
        {asDeployed ? basisSummaryLine(asDeployed) : `실제 배포 등급 ${planDbTier ?? NOT_PROVIDED}: 가격표에 없어 미제공`}
      </p>
    </div>
  );
}
