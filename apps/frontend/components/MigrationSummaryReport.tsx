"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useState } from "react";
import { StateBadge } from "@/components/StateBadge";
import { FinOpsGreenOpsCard } from "@/components/FinOpsGreenOpsCard";
import { countTone, formatBytes, isFinished, SERVING_LABEL } from "@/lib/dashboard";
import { formatTime } from "@/lib/labels";
import {
  extractCdcArcData,
  extractSourceNodeData,
  extractStageReports,
  extractTargetNodeData,
  getOverallSummaryMetrics,
} from "@/lib/topology3d";
import type { RunStatus } from "@/lib/workflows";

const JourneyCanvasView = dynamic(() => import("./3d/journey/JourneyCanvasView"), {
  ssr: false,
  loading: () => (
    <div className="flex h-[460px] w-full items-center justify-center bg-paper font-mono text-xs text-ink/50">
      3D 플라이스루 요약 리포트 불러오는 중…
    </div>
  ),
});

interface Props {
  run: RunStatus;
  standalone?: boolean;
}

// 끝나지 않았거나 실패한 run 을 완료 리포트로 부르지 않습니다.
function reportPrefix(state: RunStatus["state"]): string {
  if (state === "succeeded") return "POST-MIGRATION VISUAL REPORT";
  return isFinished(state) ? "MIGRATION REPORT · 미완료" : "MIGRATION REPORT · 진행 중";
}

const COUNT_TONE_CLASS = {
  good: "text-emerald-700",
  bad: "text-rose-700",
  neutral: "text-ink/60",
} as const;

function CountValue({ value, unit }: { value: number | null; unit: string }) {
  return (
    <span className={COUNT_TONE_CLASS[countTone(value)]}>
      {value !== null ? `${value}${unit}` : "미제공"}
    </span>
  );
}

export function MigrationSummaryReport({ run, standalone = false }: Props) {
  const [isFullscreen, setIsFullscreen] = useState(false);

  const source = extractSourceNodeData(run);
  const cdc = extractCdcArcData(run);
  const target = extractTargetNodeData(run);
  const metrics = getOverallSummaryMetrics(run);
  const stageReports = extractStageReports(run);

  const wrapperClass = isFullscreen
    ? "fixed inset-0 z-50 flex flex-col bg-paper p-4 sm:p-6 overflow-y-auto"
    : "rounded-xl border border-ink bg-white p-5 shadow-[3px_3px_0_0_var(--color-ink)]";

  return (
    <section aria-labelledby="summary-report-heading" className={wrapperClass}>
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-ink/15 pb-4">
        <div>
          <p className="font-mono text-xs text-ink/50">{reportPrefix(run.state)} · {run.run_id}</p>
          <h2 id="summary-report-heading" className="mt-0.5 text-lg font-semibold sm:text-xl">
            이전 요약 리포트 (3D 플라이스루)
          </h2>
          <p className="mt-0.5 text-xs text-ink/65">
            {source.serviceTitle} · {source.cloudName} {source.region ?? "미제공"} → {target.providerSummary} ·{" "}
            {run.state === "succeeded" ? "완료 시각" : "마지막 갱신 시각"} {formatTime(run.updated_at)}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <StateBadge state={run.state} />
          <button
            type="button"
            onClick={() => setIsFullscreen((prev) => !prev)}
            className="rounded border border-ink/25 bg-white px-2.5 py-1 text-xs font-medium text-ink hover:border-ink"
          >
            {isFullscreen ? "전체화면 닫기 ✕" : "전체화면 ⛶"}
          </button>
          {!standalone ? (
            <Link
              href={`/runs/${encodeURIComponent(run.run_id)}/report`}
              className="rounded border border-ink bg-ink px-2.5 py-1 text-xs font-medium text-paper hover:-translate-y-px"
            >
              전용 리포트 페이지 열기 ↗
            </Link>
          ) : (
            <Link
              href={`/runs/${encodeURIComponent(run.run_id)}`}
              className="rounded border border-ink bg-ink px-2.5 py-1 text-xs font-medium text-paper hover:-translate-y-px"
            >
              ← 운영 콘솔로 돌아가기
            </Link>
          )}
        </div>
      </div>

      {/* 핵심 실측 지표 요약 행 */}
      <dl className="mt-4 grid grid-cols-2 gap-2.5 sm:grid-cols-5">
        <div className="rounded-lg border border-ink/15 bg-paper p-2.5">
          <dt className="text-[11px] text-ink/55">현재 트래픽 처리</dt>
          <dd
            className={`mt-0.5 font-mono text-base font-semibold ${
              metrics.serving === "target" ? "text-emerald-700" : ""
            }`}
          >
            {metrics.serving ? SERVING_LABEL[metrics.serving] : "미제공"}
          </dd>
        </div>
        <div className="rounded-lg border border-ink/15 bg-paper p-2.5">
          <dt className="text-[11px] text-ink/55">전환 소요 (T1−T0)</dt>
          <dd className="mt-0.5 font-mono text-base font-semibold">
            {metrics.cutoverSeconds !== null ? `${metrics.cutoverSeconds.toFixed(1)}초` : "미제공"}
          </dd>
        </div>
        <div className="rounded-lg border border-ink/15 bg-paper p-2.5">
          <dt className="text-[11px] text-ink/55">행 수 (원본 / 목적지)</dt>
          <dd className="mt-0.5 font-mono text-base font-semibold">
            {metrics.rows
              ? `${metrics.rows.source.toLocaleString("ko-KR")} / ${metrics.rows.target.toLocaleString("ko-KR")}`
              : "미제공"}
          </dd>
        </div>
        <div className="rounded-lg border border-ink/15 bg-paper p-2.5">
          <dt className="text-[11px] text-ink/55">최종 복제 지연</dt>
          <dd className="mt-0.5 font-mono text-base font-semibold">
            {metrics.lagBytes !== null ? formatBytes(metrics.lagBytes) : "미제공"}
          </dd>
        </div>
        <div className="rounded-lg border border-ink/15 bg-paper p-2.5">
          <dt className="text-[11px] text-ink/55">불일치 / 실패 요청</dt>
          <dd className="mt-0.5 font-mono text-base font-semibold">
            <CountValue value={metrics.mismatch} unit="개" /> /{" "}
            <CountValue value={metrics.failedRequests} unit="건" />
          </dd>
        </div>
      </dl>

      {/* Step 2: Drei ScrollControls 기반 3D 플라이스루 여정 뷰포트 */}
      <div
        className={`mt-4 overflow-hidden rounded-lg border border-ink ${
          isFullscreen ? "flex-1 min-h-[520px]" : "h-[460px]"
        }`}
      >
        <JourneyCanvasView run={run} />
      </div>

      {/* 전용 페이지에서만 표시 */}
      {standalone && (
        <div className="mt-6 grid gap-6 md:grid-cols-2">
          <div className="rounded-lg border border-ink/15 bg-paper p-4">
            <h3 className="text-sm font-semibold">프로비저닝 & 배포 산출물 (실측)</h3>
            <dl className="mt-3 space-y-1.5 text-xs">
              <div className="flex justify-between gap-2">
                <dt className="text-ink/60">생성 VPC</dt>
                <dd className="font-mono">{target.vpcId ?? "미제공"}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-ink/60">목적지 DB 인스턴스</dt>
                <dd className="font-mono">{target.dbInstanceId ?? "미제공"}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-ink/60">목적지 DB 사설 IP</dt>
                <dd className="font-mono">{target.cloudSqlIp ?? "미제공"}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-ink/60">데이터베이스명</dt>
                <dd className="font-mono">{target.dbName ?? "미제공"}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-ink/60">목적지 앱 URL</dt>
                <dd className="truncate font-mono">{target.cloudRunUrl ?? "미제공"}</dd>
              </div>
            </dl>
          </div>

          <div className="rounded-lg border border-ink/15 bg-paper p-4">
            <h3 className="text-sm font-semibold">테이블별 CDC 정합성 대조 결과</h3>
            {cdc.tables.length === 0 ? (
              <p className="mt-3 text-xs text-ink/50">테이블 대조 지표 미제공</p>
            ) : (
              <table className="mt-3 w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-ink/15 text-ink/55">
                    <th className="pb-1.5 font-medium">테이블명</th>
                    <th className="pb-1.5 font-medium text-right">원본 행 수</th>
                    <th className="pb-1.5 font-medium text-right">목적지 행 수</th>
                    <th className="pb-1.5 font-medium text-right">판정</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-ink/10 font-mono">
                  {cdc.tables.map((t) => (
                    <tr key={t.name}>
                      <td className="py-1.5 font-semibold">{t.name}</td>
                      <td className="py-1.5 text-right">{t.sourceRows.toLocaleString("ko-KR")}</td>
                      <td className="py-1.5 text-right">{t.targetRows.toLocaleString("ko-KR")}</td>
                      <td
                        className={`py-1.5 text-right font-semibold ${
                          t.match ? "text-emerald-700" : "text-rose-700"
                        }`}
                      >
                        {t.match ? "MATCH" : "MISMATCH"} ({t.percent !== null ? `${t.percent}%` : "미제공"})
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="rounded-lg border border-ink/15 bg-white p-4 md:col-span-2">
            <h3 className="text-sm font-semibold">6단계 파이프라인 실행 감사 로그</h3>
            <ol className="mt-3 grid gap-3 sm:grid-cols-3">
              {stageReports.map((sr) => (
                <li key={sr.stage} className="rounded border border-ink/15 bg-paper p-3 text-xs">
                  <div className="flex items-center justify-between">
                    <span className="font-mono font-semibold">
                      {String(sr.index).padStart(2, "0")} {sr.label}
                    </span>
                    <StateBadge state={sr.state} />
                  </div>
                  {sr.message && <p className="mt-1.5 text-ink/70">{sr.message}</p>}
                </li>
              ))}
            </ol>
          </div>

          <div className="md:col-span-2">
            <FinOpsGreenOpsCard run={run} />
          </div>
        </div>
      )}
    </section>
  );
}
