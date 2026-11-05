// 부하 도구: 전환하는 동안 가짜 사용자처럼 계속 요청을 보내며 "실패가 있었는가"를 센다.
// MVP 판정 기준 "전환 시 사용자 요청 실패 0건" 의 근거를 만들기 위한 도구.
// 요청마다 응답 코드, 어느 쪽(aws/gcp)이 응답했는지, 걸린 시간을 기록하고 끝에 요약을 낸다.

// 사용:
//   node dev/loadcheck.mjs --url https://app.team-alpha.site --rps 10 --duration 60
//
//   --url           (필수) 대상 주소. 끝에 / 를 붙이지 않는다
//   --path          요청 경로. 기본 /health/ready (DB 확인까지 하는 읽기 요청)
//   --rps           초당 요청 수. 기본 5
//   --duration      실행 시간(초). 기본 30. 0 이면 Ctrl+C 로 멈출 때까지
//   --timeout       요청 하나의 제한 시간(초). 기본 10. 넘으면 실패로 센다
//   --max-inflight  동시에 응답을 기다리는 요청의 상한. 기본 200 (넘으면 보내지 않고 건너뜀으로 센다)
//
// 읽기(GET) 요청만 보낸다. 실제 앱의 DB에 데이터를 만들지 않으려는 것.
// 실패가 1건이라도 있으면 종료 코드가 1, 인자 오류는 2, 모두 성공이면 0.
// 마지막 줄 "RESULT {...}" 는 한 줄 JSON 이라 지표로 옮기기 쉽다.

import { parseArgs } from "node:util";
import { performance } from "node:perf_hooks";

const { values } = parseArgs({
  options: {
    url: { type: "string" },
    path: { type: "string", default: "/health/ready" },
    rps: { type: "string", default: "5" },
    duration: { type: "string", default: "30" },
    timeout: { type: "string", default: "10" },
    "max-inflight": { type: "string", default: "200" },
  },
});

const rps = Number(values.rps);
const durationSec = Number(values.duration);
const timeoutMs = Number(values.timeout) * 1000;
const maxInflight = Number(values["max-inflight"]);

if (!values.url || !/^https?:\/\//.test(values.url) || !(rps > 0) || !(durationSec >= 0) || !(timeoutMs > 0) || !(maxInflight > 0)) {
  console.error("사용: node dev/loadcheck.mjs --url https://주소 [--path /health/ready] [--rps 5] [--duration 30] [--timeout 10]");
  process.exit(2);
}

const target = values.url.replace(/\/+$/, "") + (values.path.startsWith("/") ? values.path : `/${values.path}`);
const runTag = Date.now().toString(36);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------------------
// 기록
// ---------------------------------------------------------------------------
const startedAt = performance.now();
const elapsed = () => performance.now() - startedAt; // 시작 이후 경과 시간(ms)
const records = []; // 끝난 요청들
const inflight = new Set(); // 응답을 기다리는 요청들
let sent = 0;
let skipped = 0;
let stopping = false;
process.on("SIGINT", () => {
  stopping = true;
});

// 응답이 어느 쪽에서 왔는지. 앱의 x-served-by 가 우선이고, 없으면 Worker 의 x-gate-origin 으로 짐작한다.
function sideOf(servedBy, gate) {
  if (servedBy) return servedBy;
  if (gate === "TARGET") return "gcp";
  if (gate === "SOURCE") return "aws";
  return "알 수 없음";
}

// 요청 하나를 보내고 결과를 기록한다. 실패해도 예외를 밖으로 내지 않고 "실패"로 기록한다.
function fire() {
  const n = ++sent;
  const id = `loadcheck-${runTag}-${n}`;
  const started = elapsed();
  const task = (async () => {
    const record = { n, id, started };
    try {
      const res = await fetch(target, { headers: { "X-Request-Id": id }, signal: AbortSignal.timeout(timeoutMs) });
      await res.arrayBuffer(); // 본문까지 다 받아야 "끝난 것"으로 센다
      record.status = res.status;
      record.side = sideOf(res.headers.get("x-served-by"), res.headers.get("x-gate-origin"));
    } catch (cause) {
      record.error = cause?.name === "TimeoutError" ? "시간 초과" : (cause?.cause?.code ?? cause?.message ?? "알 수 없는 오류");
      record.side = "응답 없음";
    }
    record.finished = elapsed();
    record.ms = record.finished - record.started;
    record.ok = record.status >= 200 && record.status < 300;
    records.push(record);
  })();
  inflight.add(task);
  task.finally(() => inflight.delete(task));
}

const count = (predicate) => records.filter(predicate).length;

// 1초마다 찍는 현황 한 줄
function report() {
  const okCount = count((r) => r.ok);
  console.log(
    `[${String(Math.round(elapsed() / 1000)).padStart(3)}s] 보냄 ${sent}  끝남 ${records.length}  성공 ${okCount}  실패 ${records.length - okCount}` +
      `  | aws ${count((r) => r.side === "aws")}  gcp ${count((r) => r.side === "gcp")}  | 대기 중 ${inflight.size}`,
  );
}

// ---------------------------------------------------------------------------
// 실행: 초당 rps 개씩 duration 초 동안 보낸다
// ---------------------------------------------------------------------------
console.log(`대상 ${target}  |  초당 ${rps}개, ${durationSec > 0 ? `${durationSec}초` : "Ctrl+C 까지"}, 제한 시간 ${timeoutMs / 1000}초\n`);

const intervalMs = 1000 / rps;
const endAt = durationSec > 0 ? durationSec * 1000 : Infinity;
let nextSendAt = 0;
let nextReportAt = 1000;

while (!stopping && nextSendAt < endAt) {
  const now = elapsed();
  while (nextSendAt <= now && nextSendAt < endAt) {
    if (inflight.size >= maxInflight) skipped++;
    else fire();
    nextSendAt += intervalMs;
  }
  if (now >= nextReportAt) {
    report();
    nextReportAt += 1000;
  }
  await sleep(10);
}

// 남은 요청이 끝나기를 (제한 시간까지) 기다린다
const drainUntil = elapsed() + timeoutMs + 1000;
while (inflight.size > 0 && elapsed() < drainUntil) await sleep(20);
report();

// ---------------------------------------------------------------------------
// 요약
// ---------------------------------------------------------------------------
const failed = records.filter((r) => !r.ok);
const sides = {};
for (const r of records) sides[r.side] = (sides[r.side] ?? 0) + 1;

// 전환 분석: 처음 gcp 가 응답을 끝낸 시점 이후에 "시작된" 요청이 aws 로 간 것이 있으면 되돌아간 것이다
const firstGcp = records.filter((r) => r.side === "gcp").sort((a, b) => a.finished - b.finished)[0];
const backToAws = firstGcp ? records.filter((r) => r.side === "aws" && r.started > firstGcp.finished) : [];

const sortedMs = records.filter((r) => r.ok).map((r) => r.ms).sort((a, b) => a - b);
const percentile = (p) => (sortedMs.length ? sortedMs[Math.min(sortedMs.length - 1, Math.floor((sortedMs.length * p) / 100))] : null);
const fmt = (ms) => (ms === null ? "-" : `${Math.round(ms)}ms`);

console.log("\n========== 요약 ==========");
console.log(`총 요청 ${records.length}개 (보냄 ${sent}, 건너뜀 ${skipped})`);
console.log(`성공 ${records.length - failed.length}개, 실패 ${failed.length}개`);
console.log(`응답한 쪽: ${Object.entries(sides).map(([side, n]) => `${side} ${n}`).join(", ") || "-"}`);
if (firstGcp) {
  console.log(`처음 gcp 응답: 시작 후 ${(firstGcp.finished / 1000).toFixed(1)}초 (요청 ${firstGcp.id})`);
  console.log(`gcp 응답 이후 시작된 요청 중 aws 로 간 것: ${backToAws.length}개${backToAws.length ? "  <- 전환 후 되돌아감" : ""}`);
} else {
  console.log("gcp 응답 없음 (전환이 일어나지 않았거나 응답에 x-served-by / x-gate-origin 이 없음)");
}
console.log(`지연: 중앙값 ${fmt(percentile(50))}, 95% ${fmt(percentile(95))}, 최대 ${fmt(sortedMs[sortedMs.length - 1] ?? null)}`);

if (failed.length) {
  console.log(`\n실패한 요청 (앞의 10개):`);
  for (const r of failed.slice(0, 10)) {
    console.log(`  ${r.id}  시작 ${(r.started / 1000).toFixed(2)}s  ${r.status ? `HTTP ${r.status}` : r.error}  ${fmt(r.ms)}`);
  }
}
if (skipped) console.log(`\n주의: 동시 대기 상한(${maxInflight}) 때문에 ${skipped}개를 보내지 않았습니다. 이 측정은 설정한 초당 요청 수를 다 채우지 못했습니다.`);

const verdict = failed.length === 0 && records.length > 0;
console.log(`\n판정: ${verdict ? "통과 (실패 0건)" : "실패"}`);
console.log(
  "RESULT " +
    JSON.stringify({
      total: records.length,
      failed: failed.length,
      skipped,
      sides,
      first_gcp_at_s: firstGcp ? Number((firstGcp.finished / 1000).toFixed(2)) : null,
      back_to_aws: backToAws.length,
      p50_ms: percentile(50) === null ? null : Math.round(percentile(50)),
      p95_ms: percentile(95) === null ? null : Math.round(percentile(95)),
    }),
);
process.exit(verdict ? 0 : 1);
