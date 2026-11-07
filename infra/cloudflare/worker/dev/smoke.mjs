// 로컬 시험: Worker(wrangler dev)와 가짜 원본 세 개(9001 aws, 9002 gcp, 9003 gcp2)가 떠 있는 상태에서 실행한다.
//   BASE=http://127.0.0.1:8787 TOKEN=dev-token node dev/smoke.mjs
// 규격: infra/db_migration/agent/WORKER_API.md
import assert from "node:assert/strict";

const BASE = process.env.BASE ?? "http://127.0.0.1:8787";
const TOKEN = process.env.TOKEN ?? "dev-token";
const ISO_MS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function control(path, body, { token = TOKEN } = {}) {
  const isGet = body === undefined && (path === "status" || path === "target" || path === "arbitrage" || path === "quarantine");
  const res = await fetch(`${BASE}/control/${path}`, {
    method: isGet ? "GET" : "POST",
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: res.status, body: await res.json() };
}

async function hit(path = "/") {
  const started = Date.now();
  const res = await fetch(BASE + path);
  await res.arrayBuffer();
  return {
    status: res.status,
    servedBy: res.headers.get("x-served-by"),
    gate: res.headers.get("x-gate-origin"),
    ms: Date.now() - started,
  };
}

const hits = (n) => Promise.all(Array.from({ length: n }, () => hit()));

let failed = 0;
async function check(name, fn) {
  try {
    await fn();
    console.log(`PASS  ${name}`);
  } catch (cause) {
    failed++;
    console.log(`FAIL  ${name}\n      ${cause.message.split("\n").join("\n      ")}`);
  }
}

await control("reset", {});

await check("처음 상태: SOURCE, 처리 중 0", async () => {
  const { status, body } = await control("status");
  assert.equal(status, 200);
  assert.equal(body.mode, "SOURCE");
  assert.equal(body.next_origin, "SOURCE");
  assert.equal(body.inflight_source, 0);
  assert.equal(body.held_count, 0);
});

await check("토큰이 없거나 틀리면 401", async () => {
  assert.equal((await control("status", undefined, { token: "" })).status, 401);
  assert.equal((await control("hold", { run_id: "x" }, { token: "wrong" })).status, 401);
});

await check("run_id 없이 hold 하면 400", async () => {
  assert.equal((await control("hold", {})).status, 400);
});

await check("평소 요청은 모두 SOURCE(aws) 로 간다", async () => {
  for (const r of await hits(10)) {
    assert.equal(r.status, 200);
    assert.equal(r.servedBy, "aws");
    assert.equal(r.gate, "SOURCE");
  }
});

await check("느린 요청이 나가 있는 동안 inflight_source 는 1, 끝나면 0", async () => {
  const slow = hit("/?delay_ms=1500");
  await sleep(300);
  assert.equal((await control("status")).body.inflight_source, 1);
  await slow;
  assert.equal((await control("status")).body.inflight_source, 0);
});

let heldRequests;
await check("hold: HOLD 로 바뀌고, 같은 run_id 재시도는 그대로, 다른 run_id 는 409", async () => {
  const first = await control("hold", { run_id: "job01", max_hold_ms: 5000 });
  assert.equal(first.status, 200);
  assert.equal(first.body.mode, "HOLD");
  assert.equal(first.body.next_origin, "SOURCE");
  assert.equal(first.body.run_id, "job01");
  assert.equal(first.body.held_count, 0);
  assert.match(first.body.hold_started_at, ISO_MS);
  assert.equal(first.body.released_at, null);
  const retry = await control("hold", { run_id: "job01", max_hold_ms: 5000 });
  assert.equal(retry.status, 200);
  assert.equal(retry.body.hold_started_at, first.body.hold_started_at);
  assert.equal((await control("hold", { run_id: "job02" })).status, 409);
});

await check("보류 중 들어온 요청 3개는 붙잡히고(held_count 3), AWS 로는 하나도 안 간다", async () => {
  heldRequests = hits(3);
  await sleep(400);
  const { body } = await control("status");
  assert.equal(body.mode, "HOLD");
  assert.equal(body.held_count, 3);
  assert.equal(body.inflight_source, 0);
});

await check("switch: 다른 run_id 는 409, 같은 run_id 는 next_origin 만 바뀌고 계속 HOLD", async () => {
  assert.equal((await control("switch", { run_id: "job02", to: "TARGET" })).status, 409);
  const { status, body } = await control("switch", { run_id: "job01", to: "TARGET" });
  assert.equal(status, 200);
  assert.equal(body.mode, "HOLD");
  assert.equal(body.next_origin, "TARGET");
  assert.equal((await control("switch", { run_id: "job01", to: "TARGET" })).body.next_origin, "TARGET"); // 다시 불러도 같다
});

await check("release: 다른 run_id 는 409, 같은 run_id 는 TARGET 으로 풀리고 붙잡힌 요청이 실패 없이 GCP 로 간다", async () => {
  assert.equal((await control("release", { run_id: "job02" })).status, 409);
  const { status, body } = await control("release", { run_id: "job01" });
  assert.equal(status, 200);
  assert.equal(body.mode, "TARGET");
  assert.match(body.released_at, ISO_MS);
  assert.equal(body.held_count, 3);
  for (const r of await heldRequests) {
    assert.equal(r.status, 200);
    assert.equal(r.servedBy, "gcp");
    assert.equal(r.gate, "TARGET");
    assert.ok(r.ms >= 300, `붙잡혀 있던 시간이 너무 짧다: ${r.ms}ms`);
  }
});

await check("전환 후: 요청은 모두 TARGET(gcp), hold 는 409, release 재시도는 그대로", async () => {
  for (const r of await hits(10)) {
    assert.equal(r.status, 200);
    assert.equal(r.servedBy, "gcp");
  }
  assert.equal((await control("hold", { run_id: "job03" })).status, 409);
  const again = await control("release", { run_id: "job01" });
  assert.equal(again.status, 200);
  assert.equal(again.body.mode, "TARGET");
});

await check("에이전트가 release 를 안 부르면 max_hold_ms + 10초 뒤 스스로 풀린다 (약 11초 걸림)", async () => {
  await control("reset", {});
  await control("hold", { run_id: "job04", max_hold_ms: 500 });
  const started = Date.now();
  const r = await hit(); // 보류에 걸려 기다린다
  const waited = Date.now() - started;
  assert.equal(r.status, 200);
  assert.equal(r.servedBy, "aws"); // next_origin 은 SOURCE 그대로
  assert.ok(waited >= 10_000 && waited < 13_000, `자동 해제까지 ${waited}ms`);
  const { body } = await control("status");
  assert.equal(body.mode, "SOURCE");
  assert.match(body.released_at, ISO_MS);
});

// ---- 경로가 // 로 시작해도 원본 호스트에 머문다 ----
// 예전 방식(new URL(경로, base))은 //호스트/경로 를 "다른 호스트"로 해석해서 요청이 엉뚱한 곳으로 나갔다.
await check("경로가 //다른호스트/... 이어도 요청은 설정된 원본(aws)으로만 간다", async () => {
  await control("reset", {});
  for (const path of ["//localhost:9003/x", "//127.0.0.1:9003/x", "///localhost:9003/x"]) {
    const res = await fetch(BASE + path);
    await res.arrayBuffer();
    assert.equal(res.headers.get("x-served-by"), "aws", `${path} -> ${res.headers.get("x-served-by")}`);
  }
});

// ---- target 명령 (규격 밖 보조 기능): GCP 쪽 원본 주소를 실행 중에 바꾼다. 세 번째 가짜 원본 9003(gcp2) 필요 ----
const targetUrl = async () => (await control("target")).body.url;

await check("target: 처음엔 null, 토큰 없으면 401", async () => {
  await control("reset", {});
  assert.equal(await targetUrl(), null);
  assert.equal((await control("target", { url: "http://127.0.0.1:9003" }, { token: "" })).status, 401);
  assert.equal((await control("target", { url: "http://127.0.0.1:9003" }, { token: "wrong" })).status, 401);
});

await check("target: 잘못된 주소는 400 이고 값이 바뀌지 않는다", async () => {
  const bad = [
    {},
    { url: 123 },
    { url: "not a url" },
    { url: "http://evil.example.com" }, // http 는 localhost 만
    { url: "https://evil.example.com" }, // 허용 목록(.run.app) 밖
    { url: "https://x.run.app.evil.com" }, // 끝이 .run.app 이 아님
    { url: "https://user:pw@x.run.app" }, // 계정 정보
    { url: "https://x.run.app/path" }, // 경로
    { url: "https://x.run.app/?a=1" }, // 쿼리
  ];
  for (const body of bad) {
    const r = await control("target", body);
    assert.equal(r.status, 400, `${JSON.stringify(body)} -> ${r.status}`);
  }
  assert.equal(await targetUrl(), null);
});

await check("target: 정상 주소는 저장되고(끝 / 제거), status 응답(규격)에는 나오지 않는다", async () => {
  const r = await control("target", { url: "http://127.0.0.1:9003/" });
  assert.equal(r.status, 200);
  assert.equal(r.body.url, "http://127.0.0.1:9003");
  assert.equal(await targetUrl(), "http://127.0.0.1:9003");
  const status = (await control("status")).body;
  assert.equal("target_url" in status, false);
  assert.equal("max_hold_ms" in status, false);
  assert.equal((await control("target", { url: "https://target-app-abc123-du.a.run.app" })).status, 200); // 허용 목록
  assert.equal((await control("target", { url: "http://127.0.0.1:9003" })).status, 200);
});

await check("target: 전환(switch/release) 뒤 요청은 새 주소(gcp2)로 가고, 전환 전에는 계속 aws", async () => {
  assert.equal((await hit()).servedBy, "aws");
  await control("hold", { run_id: "job05", max_hold_ms: 5000 });
  await control("switch", { run_id: "job05", to: "TARGET" });
  await control("release", { run_id: "job05" });
  for (const r of await hits(5)) {
    assert.equal(r.status, 200);
    assert.equal(r.servedBy, "gcp2");
    assert.equal(r.gate, "TARGET");
  }
});

await check("target: 전환(TARGET) 뒤에는 바꾸려 하면 409 이고 값은 그대로", async () => {
  assert.equal((await control("target", { url: "http://127.0.0.1:9002" })).status, 409);
  assert.equal((await control("target", { url: null })).status, 409);
  assert.equal(await targetUrl(), "http://127.0.0.1:9003");
});

await check("target: reset 하면 비워지고, 다시 전환하면 설정값 UPSTREAM_TARGET(gcp) 으로 간다", async () => {
  await control("reset", {});
  assert.equal(await targetUrl(), null);
  await control("hold", { run_id: "job06", max_hold_ms: 5000 });
  await control("switch", { run_id: "job06", to: "TARGET" });
  await control("release", { run_id: "job06" });
  assert.equal((await hit()).servedBy, "gcp");
});

await check("target: {\"url\": null} 은 설정값으로 되돌린다", async () => {
  await control("reset", {});
  assert.equal((await control("target", { url: "http://127.0.0.1:9003" })).status, 200);
  assert.equal((await control("target", { url: null })).status, 200);
  assert.equal(await targetUrl(), null);
});

// ---- request_counts: hold 부터 끝난 요청을 쓰기·읽기로 나눠 센다 ----
const write = async (path = "/") => {
  const res = await fetch(BASE + path, { method: "POST", body: "{}" });
  await res.arrayBuffer();
  return res.status;
};

await check("request_counts: 쓰기·읽기와 5xx 를 나눠 세고, 4xx 는 실패가 아니다", async () => {
  await control("reset", {});
  assert.equal(await write(), 200);
  assert.equal(await write("/?status=503"), 503);
  assert.equal(await write("/?status=404"), 404);
  assert.equal((await hit()).status, 200);
  assert.equal((await hit("/?status=500")).status, 500);
  const { since, ...counts } = (await control("status")).body.request_counts;
  assert.match(since, ISO_MS);
  assert.deepEqual(counts, { write_total: 3, write_failed: 1, read_total: 2, read_failed: 1 });
});

await check("request_counts: 새 hold 에서 0 으로 시작하고 since 가 hold_started_at 과 같다", async () => {
  const first = await control("hold", { run_id: "job07", max_hold_ms: 5000 });
  assert.equal(first.body.request_counts.since, first.body.hold_started_at);
  assert.equal(first.body.request_counts.write_total, 0);
  const retry = await control("hold", { run_id: "job07", max_hold_ms: 5000 });
  assert.equal(retry.body.request_counts.since, first.body.hold_started_at); // 재시도는 다시 세지 않는다
});

await check("request_counts: 보류 중 들어온 쓰기는 풀린 뒤 끝날 때 센다", async () => {
  const held = write();
  await sleep(300);
  assert.equal((await control("status")).body.request_counts.write_total, 0);
  await control("release", { run_id: "job07" });
  assert.equal(await held, 200);
  assert.equal((await control("status")).body.request_counts.write_total, 1);
});

await check("request_counts: 원본에 닿지 못하면 실패로 센다", async () => {
  await control("reset", {});
  await control("target", { url: "http://127.0.0.1:9" }); // 아무도 듣지 않는 포트
  await control("hold", { run_id: "job08", max_hold_ms: 5000 });
  await control("switch", { run_id: "job08", to: "TARGET" });
  await control("release", { run_id: "job08" });
  assert.equal(await write(), 502);
  const { request_counts: counts } = (await control("status")).body;
  assert.equal(counts.write_total, 1);
  assert.equal(counts.write_failed, 1);
});

await check("arbitrage: 동적 차익거래 설정, 읽기 우회, 쓰기 마스터 펜싱, 복제 지연 세이프가드", async () => {
  await control("reset", {});
  // 1. 기본 상태 조회
  const init = await control("arbitrage");
  assert.equal(init.status, 200);
  assert.equal(init.body.enabled, false);

  // 2. 차익거래 활성화 (문자열 'true' 입력 및 current_lag_bytes: 0)
  const setRes = await control("arbitrage", { enabled: "true", read_target_weight: 1.0, max_lag_bytes: 1024, current_lag_bytes: 0 });
  assert.equal(setRes.status, 200);
  assert.equal(setRes.body.enabled, true);
  assert.equal(setRes.body.read_target_weight, 1.0);

  // 3. 읽기 요청: TARGET으로 우회되고 X-Arbitrage 헤더 부여
  const readRes = await fetch(BASE + "/");
  assert.equal(readRes.headers.get("x-gate-origin"), "TARGET");
  assert.equal(readRes.headers.get("x-arbitrage"), "true");

  // 4. 쓰기 요청: 읽기 우회가 켜져 있어도 항상 SOURCE(마스터)로 100% 펜싱
  const writeRes = await fetch(BASE + "/", { method: "POST", body: "{}" });
  assert.equal(writeRes.headers.get("x-gate-origin"), "SOURCE");
  assert.equal(writeRes.headers.get("x-arbitrage"), null);

  // 5. 복제 지연 세이프가드 (Fail-Closed):
  // 5-1. 제어 평면의 CDC lag가 1024B를 넘으면 즉시 SOURCE로 펜싱
  await control("arbitrage", { enabled: true, read_target_weight: 1.0, max_lag_bytes: 1024, current_lag_bytes: 4096 });
  const laggedControlRead = await fetch(BASE + "/");
  assert.equal(laggedControlRead.headers.get("x-gate-origin"), "SOURCE");
  assert.equal(laggedControlRead.headers.get("x-arbitrage"), null);

  // 5-2. 제어 평면 lag가 정상이더라도 클라이언트가 보고한 지연이 크면 보수적으로 SOURCE로 펜싱
  await control("arbitrage", { enabled: true, read_target_weight: 1.0, max_lag_bytes: 1024, current_lag_bytes: 100 });
  const laggedClientRead = await fetch(BASE + "/", {
    headers: { "X-Replication-Lag-Bytes": "2048" },
  });
  assert.equal(laggedClientRead.headers.get("x-gate-origin"), "SOURCE");
  assert.equal(laggedClientRead.headers.get("x-arbitrage"), null);

  // 6. enabled: "false" 입력 시 확실히 비활성화되는지 검증
  await control("arbitrage", { enabled: "false" });
  assert.equal((await control("arbitrage")).body.enabled, false);
});

await check("quarantine: 보안 침해 노드 격리, 읽기 우회, 쓰기 차단(503) 및 롤백 해제", async () => {
  await control("reset", {});

  // 1. 초기 격리 상태 조회
  const init = await control("quarantine");
  assert.equal(init.status, 200);
  assert.deepEqual(init.body.isolated_nodes, []);

  // 2. 침해 노드 격리 설정 (null 및 빈 문자열 무시 검증)
  const setRes = await control("quarantine", {
    isolated_nodes: ["aws-ecs-workitems", null, ""],
    reroute_target: "gcp-cloud-run",
  });
  assert.equal(setRes.status, 200);
  assert.deepEqual(setRes.body.isolated_nodes, ["aws-ecs-workitems"]);

  // 3. 읽기 요청: TARGET으로 우회되고 X-Quarantine 헤더 부여
  const readRes = await fetch(BASE + "/");
  assert.equal(readRes.headers.get("x-gate-origin"), "TARGET");
  assert.equal(readRes.headers.get("x-quarantine"), "true");

  // 4. 쓰기 요청: 침해 상태에서 복제본 쓰기로 인한 split-brain 방지를 위해 503 차단
  const writeRes = await fetch(BASE + "/", { method: "POST", body: "{}" });
  assert.equal(writeRes.status, 503);
  const writeBody = await writeRes.json();
  assert.match(writeBody.error, /quarantine/);

  // 5. 격리 해제 (빈 배열 전송)
  const releaseRes = await control("quarantine", { isolated_nodes: [] });
  assert.equal(releaseRes.status, 200);
  assert.deepEqual(releaseRes.body.isolated_nodes, []);

  // 6. 정상 복귀 확인: 읽기/쓰기 모두 SOURCE로 정상 복구
  const restoredRead = await fetch(BASE + "/");
  assert.equal(restoredRead.headers.get("x-gate-origin"), "SOURCE");
  assert.equal(restoredRead.headers.get("x-quarantine"), null);

  const restoredWrite = await fetch(BASE + "/", { method: "POST", body: "{}" });
  assert.equal(restoredWrite.status, 200);
  assert.equal(restoredWrite.headers.get("x-gate-origin"), "SOURCE");
});

await control("reset", {});

console.log(failed === 0 ? "\n모두 통과" : `\n실패 ${failed}건`);
process.exit(failed === 0 ? 0 : 1);
