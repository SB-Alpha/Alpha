export { Gate } from "./gate.js";

const DEFAULT_MAX_HOLD_MS = 3_000; 
const MAX_HOLD_MS_LIMIT = 60_000; 
const MODES = new Set(["SOURCE", "TARGET"]);
const READ_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

const DEFAULT_ALLOWED_TARGET_SUFFIXES = [".run.app"];
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1"]);

// parseTargetUrl: 받은 주소를 검증하고 정리한다. 돌려주는 값: { url } 또는 { error }
function parseTargetUrl(value, env) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return { error: "url is not a valid URL" };
  }
  const local = LOCAL_HOSTS.has(parsed.hostname);
  if (parsed.protocol !== "https:" && !(local && parsed.protocol === "http:")) {
    return { error: "url must be https (http is allowed only for localhost)" };
  }
  if (parsed.username || parsed.password) return { error: "url must not contain credentials" };
  if (parsed.search || parsed.hash) return { error: "url must not contain a query or fragment" };
  if (parsed.pathname !== "/") return { error: "url must not contain a path" };
  const configured = (env.TARGET_ALLOWED_SUFFIXES ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  const allowed = configured.length ? configured : DEFAULT_ALLOWED_TARGET_SUFFIXES;
  if (!local && !allowed.some((suffix) => parsed.hostname.endsWith(suffix))) {
    return { error: `host must end with one of: ${allowed.join(", ")}` };
  }
  return { url: parsed.origin }; // 끝의 / 를 뗀 "https://호스트" 모양
}

const json = (body, status = 200) => Response.json(body, { status });
const error = (status, message) => json({ error: message }, status);

async function readJson(request) {
  try {
    const body = await request.json();
    return body && typeof body === "object" ? body : null;
  } catch {
    return null;
  }
}


async function control(request, url, gate, env) {
  if (!env.CONTROL_TOKEN || request.headers.get("Authorization") !== `Bearer ${env.CONTROL_TOKEN}`) {
    return error(401, "unauthorized");
  }

  const command = url.pathname.slice("/control/".length);
  const respond = (result) => json(result.body, result.status);

  if (command === "status") {
    if (request.method !== "GET") return error(405, "method not allowed");
    return respond(await gate.status());
  }

  // 규격 밖 보조 명령: GCP 쪽 원본 주소 조회(GET)와 변경(POST). {"url": null} 이면 설정값으로 되돌린다.
  if (command === "target") {
    if (request.method === "GET") return respond(await gate.getTarget());
    if (request.method !== "POST") return error(405, "method not allowed");
    const body = await readJson(request);
    if (!body || !("url" in body)) return error(400, 'url is required (use {"url": null} to clear it)');
    if (body.url === null) return respond(await gate.setTarget({ url: null }));
    if (typeof body.url !== "string") return error(400, "url must be a string or null");
    const parsed = parseTargetUrl(body.url, env);
    if (parsed.error) return error(400, parsed.error);
    return respond(await gate.setTarget({ url: parsed.url }));
  }

  if (!["hold", "switch", "release", "reset"].includes(command)) return error(404, "not found");
  if (request.method !== "POST") return error(405, "method not allowed");
  if (command === "reset") return respond(await gate.reset());

  const body = await readJson(request);
  if (!body || typeof body.run_id !== "string" || body.run_id === "") return error(400, "run_id is required");

  if (command === "hold") {
    const maxHoldMs = body.max_hold_ms ?? DEFAULT_MAX_HOLD_MS;
    if (!Number.isInteger(maxHoldMs) || maxHoldMs < 1 || maxHoldMs > MAX_HOLD_MS_LIMIT) {
      return error(400, `max_hold_ms must be an integer between 1 and ${MAX_HOLD_MS_LIMIT}`);
    }
    return respond(await gate.hold({ run_id: body.run_id, max_hold_ms: maxHoldMs }));
  }
  if (command === "switch") {
    const to = body.to ?? "TARGET"; // 안 보내면 TARGET(GCP)
    if (!MODES.has(to)) return error(400, "to must be SOURCE or TARGET");
    return respond(await gate.switchTo({ run_id: body.run_id, to }));
  }

  return respond(await gate.release({ run_id: body.run_id }));
}


// Cloudflare 가 요청이 들어올 때마다 이 fetch 를 호출
// request : 들어온 요청 / env : 설정 값 모음
export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // 게이트에게 연결한다. 항상 "main" 이라는 이름의 같은 게이트 하나를 씀.
    // 전 세계 어디서 요청이 들어와도 같은 게이트를 봄.
    const gate = env.GATE.get(env.GATE.idFromName("main"));

    if (url.pathname.startsWith("/control/")) return control(request, url, gate, env);

    const isWrite = !READ_METHODS.has(request.method);
    let isFailure = true; // 원본 응답을 받기 전에 끝나면 실패로 센다
    const { origin, ticket, target } = await gate.enter();
    try {
      // TARGET 이면 제어 명령으로 정한 주소(target)를 먼저 쓰고, 없으면 설정값 UPSTREAM_TARGET 을 쓴다
      const base = origin === "TARGET" ? target || env.UPSTREAM_TARGET : env.UPSTREAM_SOURCE;
      if (!base) return error(500, `upstream for ${origin} is not configured`);

      // 경로를 new URL(경로, base) 로 합치면 "//호스트/경로" 를 다른 호스트로 해석해서 요청이 엉뚱한 곳으로 나간다.
      // 그래서 base 의 호스트는 그대로 두고 경로와 쿼리만 바꿔 끼운다.
      const destination = new URL(base);
      destination.pathname = url.pathname;
      destination.search = url.search;
      const upstream = new Request(destination, request);
      upstream.headers.set("X-Forwarded-Host", url.host);
      // 프록시 확인 비밀은 목적지에만 보낸다. 원본(AWS)으로 가면 원본 쪽 로그에 남을 수 있다.
      // 클라이언트가 같은 헤더를 보내도 그대로 넘기지 않는다.
      upstream.headers.delete("X-Alpha-Worker-Secret");
      if (origin === "TARGET" && env.WORKER_SECRET) {
        upstream.headers.set("X-Alpha-Worker-Secret", env.WORKER_SECRET);
      }

      const response = await fetch(upstream, { redirect: "manual" });
      isFailure = response.status >= 500;

      const out = new Response(response.body, response);
      out.headers.set("X-Gate-Origin", origin);
      return out;
    } catch (cause) {
      return error(502, `upstream request failed: ${cause?.message ?? "unknown"}`);
    } finally {
      await gate.exit(ticket, { isWrite, isFailure });
    }
  },
};
