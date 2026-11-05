// 로컬 시험: Worker(wrangler dev)와 가짜 원본 두 개(9001 aws, 9002 gcp)가 떠 있는 상태에서 실행한다.
//   BASE=http://127.0.0.1:8787 TOKEN=dev-token node dev/smoke.mjs
// 규격: infra/db_migration/agent/WORKER_API.md
import assert from "node:assert/strict";

const BASE = process.env.BASE ?? "http://127.0.0.1:8787";
const TOKEN = process.env.TOKEN ?? "dev-token";
const ISO_MS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function control(path, body, { token = TOKEN } = {}) {
  const res = await fetch(`${BASE}/control/${path}`, {
    method: body === undefined && path === "status" ? "GET" : "POST",
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

await control("reset", {});

console.log(failed === 0 ? "\n모두 통과" : `\n실패 ${failed}건`);
process.exit(failed === 0 ? 0 : 1);
