// 실시간 에지·토폴로지 API 클라이언트 (backend app/features/workflows/live.py · topology.py 대응).
// 값을 모르면 null 로 둡니다. 화면은 null 을 "미제공"으로 보여 주고, 숫자를 지어내지 않습니다.

import { useEffect, useRef, useState } from "react";

export type LiveSourceStatus = "ok" | "not_configured" | "error";
/** skipped: 게이트가 HOLD 이거나 상태를 몰라 프로브를 보내지 않음 (전환 측정에 섞이지 않게). */
export type ProbeStatus = LiveSourceStatus | "skipped";

/** Worker 누적 요청 수. 이 다섯 키가 모두 오거나 객체 전체가 null 입니다. */
export interface WorkerRequestCounts {
  since: string | null;
  write_total: number | null;
  write_failed: number | null;
  read_total: number | null;
  read_failed: number | null;
}

export interface WorkerLive {
  status: LiveSourceStatus;
  error: string | null;
  mode: string | null; // "SOURCE" | "HOLD" | "TARGET" (gate.js)
  next_origin: string | null; // "SOURCE" | "TARGET"
  hold_started_at: string | null;
  released_at: string | null;
  held_count: number | null;
  inflight_source: number | null;
  request_counts: WorkerRequestCounts | null;
  /** [] 은 격리 없음, null 은 모름입니다. */
  isolated_nodes: string[] | null;
}

export interface AgentLive {
  status: LiveSourceStatus;
  error: string | null;
  kafka: boolean | null;
  connect: boolean | null;
  plugins_missing: string[] | null;
}

export interface ProbeLive {
  status: ProbeStatus;
  error: string | null;
  /** status "ok" 인데 ok:false 면 요청은 보냈고 서비스가 실패를 돌려준 것입니다 (지연은 측정됨). */
  ok: boolean | null;
  latency_ms: number | null;
  origin: string | null; // X-Gate-Origin: "SOURCE" | "TARGET"
}

export interface EdgeSnapshot {
  checked_at: string;
  worker: WorkerLive;
  agent: AgentLive;
  probe: ProbeLive;
}

export type TopologyEdgeKind = "app_db" | "replication" | "traffic";

export interface TopologyNode {
  id: string;
  kind: string;
  label: string;
  cloud: string | null;
  region: string | null;
  resource: string | null;
  /** 이 노드를 맡은 단계의 상태입니다. 리소스 헬스가 아닙니다. */
  state: string | null;
}

export interface TopologyEdge {
  id: string;
  from: string;
  to: string;
  kind: TopologyEdgeKind;
  via: "vpn" | null;
}

export interface Topology {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
}

/** 초당 요청 수. 각 값은 해당 누적값을 모르면 null 입니다. */
export interface RequestRate {
  writePerSec: number | null;
  readPerSec: number | null;
}

/** 비율 계산에 쓰는 한 번의 관측. checkedAt 은 백엔드 스냅샷 시각입니다. */
export interface RateSample {
  checkedAt: string;
  counts: WorkerRequestCounts | null;
}

function perSecond(prev: number | null, next: number | null, seconds: number): number | null | "reset" {
  if (prev === null || next === null) return null;
  const delta = next - prev;
  if (delta < 0) return "reset";
  return delta / seconds;
}

/**
 * Worker 누적 요청 수 두 개로 초당 비율을 계산합니다.
 * 첫 관측, 집계 구간(since) 변경, 누적값 감소, 시각이 같거나 거꾸로면 null 입니다 (추정하지 않음).
 */
export function rateFromCounts(prev: RateSample | null, next: RateSample | null): RequestRate | null {
  if (!prev || !next || !prev.counts || !next.counts) return null;
  if (prev.counts.since !== next.counts.since) return null;
  const seconds = (Date.parse(next.checkedAt) - Date.parse(prev.checkedAt)) / 1000;
  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  const write = perSecond(prev.counts.write_total, next.counts.write_total, seconds);
  const read = perSecond(prev.counts.read_total, next.counts.read_total, seconds);
  if (write === "reset" || read === "reset") return null;
  return { writePerSec: write, readPerSec: read };
}

/** 쓰기·읽기 중 아는 값의 합. 둘 다 모르면 null 입니다. */
export function totalRate(rate: RequestRate | null): number | null {
  if (!rate) return null;
  if (rate.writePerSec === null && rate.readPerSec === null) return null;
  return (rate.writePerSec ?? 0) + (rate.readPerSec ?? 0);
}

export type LiveTrafficPhase = "source" | "hold" | "switch" | "target" | "unknown";

/** Worker 가 보고한 게이트 상태로만 정합니다. Worker 가 ok 가 아니면 unknown 입니다. */
export function liveTrafficPhase(worker: WorkerLive | null | undefined): LiveTrafficPhase {
  if (!worker || worker.status !== "ok") return "unknown";
  if (worker.mode === "SOURCE") return "source";
  if (worker.mode === "TARGET") return "target";
  if (worker.mode === "HOLD") return worker.next_origin === "TARGET" ? "switch" : "hold";
  return "unknown";
}

const isHidden = () => typeof document !== "undefined" && document.hidden;

async function fetchJson<T>(url: string, signal: AbortSignal): Promise<T | null> {
  const res = await fetch(url, { cache: "no-store", signal });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

/**
 * url 을 겹치지 않게 폴링합니다. 응답을 받은 뒤에 다음 요청을 예약하고, 탭이 숨겨지면 멈췄다가 다시 보이면 이어 갑니다.
 * onData 는 응답을 받을 때마다 불립니다.
 */
function usePolling<T>(
  url: string | null,
  intervalMs: number,
  onData: (data: T) => void,
  onError: (msg: string) => void,
  onRestart?: () => void,
) {
  const onDataRef = useRef(onData);
  const onErrorRef = useRef(onError);
  const onRestartRef = useRef(onRestart);
  useEffect(() => {
    onDataRef.current = onData;
    onErrorRef.current = onError;
    onRestartRef.current = onRestart;
  });

  useEffect(() => {
    if (!url) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let isStopped = false;
    let isInFlight = false;

    const tick = async () => {
      timer = undefined;
      if (isStopped || isInFlight) return;
      if (isHidden()) return; // visibilitychange 에서 다시 시작합니다.
      isInFlight = true;
      try {
        const data = await fetchJson<T>(url, controller.signal);
        if (!isStopped && data !== null) onDataRef.current(data);
      } catch (cause) {
        if (!isStopped) onErrorRef.current(cause instanceof Error ? cause.message : String(cause));
      } finally {
        isInFlight = false;
      }
      if (!isStopped && !isHidden()) timer = setTimeout(tick, intervalMs);
    };

    const onVisibility = () => {
      if (isHidden() || timer !== undefined || isInFlight) return;
      // 숨겨진 동안 폴링이 실제로 멈췄습니다. 그 전 관측은 버리고 새로 시작합니다.
      onRestartRef.current?.();
      void tick();
    };

    void tick();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      isStopped = true;
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      // 폴링이 꺼지면 마지막 값을 남기지 않습니다. 다시 켰을 때 예전 값이 실시간처럼 보이지 않게 합니다.
      onRestartRef.current?.();
    };
  }, [url, intervalMs]);
}

export interface EdgeLiveState {
  snapshot: EdgeSnapshot | null;
  /** Worker 누적 요청 수로 계산한 초당 비율. 계산할 수 없으면 null 입니다. */
  rate: RequestRate | null;
  /** /api/live/edge 호출 자체가 실패했을 때의 표시용 문구입니다. */
  fetchError: string | null;
}

/** GET /api/live/edge 를 폴링합니다. 백엔드가 1초 캐시하므로 같은 checked_at 이면 비율을 갱신하지 않습니다. */
export function useEdgeLive(intervalMs = 1500, enabled = true): EdgeLiveState {
  const [state, setState] = useState<EdgeLiveState>({ snapshot: null, rate: null, fetchError: null });
  const prevSampleRef = useRef<RateSample | null>(null);

  usePolling<EdgeSnapshot>(
    enabled ? "/api/live/edge" : null,
    intervalMs,
    (snapshot) => {
      const prev = prevSampleRef.current;
      if (prev && prev.checkedAt === snapshot.checked_at) {
        setState((s) => ({ ...s, snapshot, fetchError: null }));
        return;
      }
      const counts = snapshot.worker.status === "ok" ? snapshot.worker.request_counts : null;
      const next: RateSample = { checkedAt: snapshot.checked_at, counts };
      prevSampleRef.current = next;
      setState({ snapshot, rate: rateFromCounts(prev, next), fetchError: null });
    },
    (message) => {
      // 연결이 끊기면 이전 관측과 이어서 비율을 계산하지 않습니다.
      prevSampleRef.current = null;
      setState({ snapshot: null, rate: null, fetchError: message });
    },
    () => {
      // 폴링이 멈췄다 다시 시작하면 멈춘 동안의 평균을 '지금 초당 비율'로 보여 주지 않습니다.
      prevSampleRef.current = null;
      setState({ snapshot: null, rate: null, fetchError: null });
    },
  );

  return enabled ? state : { snapshot: null, rate: null, fetchError: null };
}

export interface RunTopologyState {
  topology: Topology | null;
  fetchError: string | null;
}

/** GET /api/runs/{id}/topology 를 5초마다 폴링합니다. */
export function useRunTopology(runId: string, enabled = true): RunTopologyState {
  const [state, setState] = useState<RunTopologyState>({ topology: null, fetchError: null });
  usePolling<Topology>(
    enabled ? `/api/runs/${encodeURIComponent(runId)}/topology` : null,
    5000,
    (topology) => setState({ topology, fetchError: null }),
    (message) => setState((s) => ({ topology: s.topology, fetchError: message })),
    () => setState({ topology: null, fetchError: null }),
  );
  return enabled ? state : { topology: null, fetchError: null };
}
