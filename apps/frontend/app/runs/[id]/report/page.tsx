"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { MigrationSummaryReport } from "@/components/MigrationSummaryReport";
import { workflows, type RunStatus } from "@/lib/workflows";

function RunReportContent() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<RunStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let isCurrent = true;
    workflows
      .get(id)
      .then((data) => {
        if (isCurrent) {
          setRun(data);
          setError(null);
        }
      })
      .catch((err) => {
        if (isCurrent) {
          setError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      isCurrent = false;
    };
  }, [id]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <Link href={`/runs/${encodeURIComponent(id)}`} className="text-sm text-ink/60 hover:text-ink">
          ← 이전 관제 콘솔로 돌아가기
        </Link>
        <Link href="/" className="text-sm text-ink/60 hover:text-ink">
          전체 run 목록
        </Link>
      </div>

      {error && (
        <p role="alert" className="rounded-md border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error}
        </p>
      )}

      {run === null ? (
        !error && <p className="text-ink/50">요약 리포트를 불러오는 중…</p>
      ) : (
        <MigrationSummaryReport run={run} standalone={true} />
      )}
    </div>
  );
}

export default function RunReportPage() {
  return (
    <Suspense fallback={<p className="text-ink/50">요약 리포트를 불러오는 중…</p>}>
      <RunReportContent />
    </Suspense>
  );
}
