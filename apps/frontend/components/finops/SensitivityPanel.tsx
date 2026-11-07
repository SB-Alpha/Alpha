"use client";

import { useMemo } from "react";
import {
  NOT_PROVIDED,
  computeSensitivity,
  findFlipPoints,
  flipPointLine,
  formatUsd,
  signedPercent,
  signedUsd,
  type FinOpsInputParams,
  type FlipPoint,
} from "@/lib/finops";

interface Props {
  params: FinOpsInputParams;
}

function FlipList({ title, flips }: { title: string; flips: FlipPoint[] }) {
  return (
    <div className="mt-2">
      <p className="font-medium text-ink">{title}</p>
      {/* 등급이 가격표에 없으면 스캔하지 않습니다. "뒤집히지 않음"으로 표시하면 근거 없는 주장이 됩니다. */}
      {flips.length === 0 ? (
        <p className="mt-0.5">{NOT_PROVIDED}</p>
      ) : (
        <ul className="mt-0.5 space-y-0.5">
          {flips.map((flip) => (
            <li key={flip.axis}>{flipPointLine(flip)}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function SensitivityPanel({ params }: Props) {
  const sameMemoryFlips = useMemo(() => findFlipPoints({ ...params, dbBasis: "same_memory" }), [params]);
  const asDeployedFlips = useMemo(() => findFlipPoints({ ...params, dbBasis: "as_deployed" }), [params]);
  const rows = useMemo(() => computeSensitivity(params), [params]);

  return (
    <div className="mt-4 grid gap-4 rounded-lg border border-ink/15 p-4 text-xs text-ink/75 lg:grid-cols-2">
      <div>
        <h3 className="font-semibold text-ink">결론이 뒤집히는 지점 (공시가 계산, 실측 트래픽 아님)</h3>
        <FlipList title="같은 메모리 등급" flips={sameMemoryFlips} />
        <FlipList title="실제 배포 등급" flips={asDeployedFlips} />
      </div>

      <div className="min-w-0">
        <h3 className="font-semibold text-ink">입력을 바꾸면</h3>
        <p className="mt-0.5 text-[11px] text-ink/60">같은 메모리 등급 기준의 공시가 계산이며 실측이 아닙니다. 다른 입력은 현재값으로 둡니다.</p>
        <div className="mt-2 overflow-x-auto">
          <table className="w-full min-w-[420px] border-collapse font-mono text-[11px] tabular-nums">
            <thead>
              <tr className="border-b border-ink/20 text-left text-ink/60">
                <th scope="col" className="py-1 pr-2 font-medium">입력</th>
                <th scope="col" className="py-1 pr-2 text-right font-medium">AWS/월</th>
                <th scope="col" className="py-1 pr-2 text-right font-medium">목적지/월</th>
                <th scope="col" className="py-1 text-right font-medium">증감</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.label} className="border-b border-ink/10 last:border-b-0">
                  <th scope="row" className="py-1 pr-2 text-left font-normal">{row.label}</th>
                  <td className="py-1 pr-2 text-right">{formatUsd(row.sourceTotal)}</td>
                  <td className="py-1 pr-2 text-right">{formatUsd(row.targetTotal)}</td>
                  <td className="py-1 text-right text-ink">
                    {signedPercent(row.deltaPercent)} ({signedUsd(row.monthlyDeltaUsd)})
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
