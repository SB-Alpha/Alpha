// timings API 클라이언트 (backend app/features/workflows/timings.py 대응).
// AI 환각 없이 저장된 타임스탬프로부터 계산된 단계별 실측 소요 시간과 과거 통계를 가져옵니다.

import { useEffect, useState } from "react";
import type { StageName } from "./workflows";

export interface StageTiming {
  stage: StageName;
  seconds: number | null;
  note: string | null;
}

export interface RunTimings {
  run_id: string;
  stages: StageTiming[];
  measured_total_seconds: number | null;
  approval_wait_seconds: number | null;
}

export interface StageBaseline {
  stage: StageName;
  samples: number;
  min_seconds: number | null;
  median_seconds: number | null;
  max_seconds: number | null;
}

export interface TimingBaseline {
  runs_considered: number;
  stages: StageBaseline[];
  note: string;
}

export async function fetchRunTimings(runId: string): Promise<RunTimings | null> {
  try {
    const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/timings`);
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export async function fetchTimingBaseline(): Promise<TimingBaseline | null> {
  try {
    const res = await fetch("/api/runs/stats/timings");
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export function formatTimingSeconds(sec: number | null | undefined): string {
  if (typeof sec !== "number" || !Number.isFinite(sec)) return "—";
  if (sec < 60) return `${sec.toFixed(1)}초`;
  const mins = Math.floor(sec / 60);
  const remSec = (sec % 60).toFixed(0);
  return `${mins}분 ${remSec}초`;
}

/**
 * 특정 run 의 단계별 실측 시간 및 과거 통계 베이스라인을 주기적으로 조회하는 훅
 */
export function useRunTimings(runId: string, shouldPoll: boolean = true) {
  const [timings, setTimings] = useState<RunTimings | null>(null);
  const [baseline, setBaseline] = useState<TimingBaseline | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);

  useEffect(() => {
    let mounted = true;

    async function load() {
      const [t, b] = await Promise.all([
        fetchRunTimings(runId),
        fetchTimingBaseline(),
      ]);
      if (mounted) {
        if (t) setTimings(t);
        if (b) setBaseline(b);
        setIsLoading(false);
      }
    }

    void load();

    if (!shouldPoll) return () => { mounted = false; };

    const interval = setInterval(() => {
      void load();
    }, 5000);

    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [runId, shouldPoll]);

  return { timings, baseline, isLoading };
}
