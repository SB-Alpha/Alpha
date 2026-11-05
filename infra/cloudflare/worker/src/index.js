export { Gate } from "./gate.js";

const DEFAULT_MAX_HOLD_MS = 3_000; 
const MAX_HOLD_MS_LIMIT = 60_000; 
const MODES = new Set(["SOURCE", "TARGET"]);

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

    const { origin, ticket } = await gate.enter();
    try {
      const base = origin === "TARGET" ? env.UPSTREAM_TARGET : env.UPSTREAM_SOURCE;
      if (!base) return error(500, `upstream for ${origin} is not configured`);

      const upstream = new Request(new URL(url.pathname + url.search, base), request);
      upstream.headers.set("X-Forwarded-Host", url.host);

      const response = await fetch(upstream, { redirect: "manual" });

      const out = new Response(response.body, response);
      out.headers.set("X-Gate-Origin", origin);
      return out;
    } catch (cause) {
      return error(502, `upstream request failed: ${cause?.message ?? "unknown"}`);
    } finally {
      await gate.exit(ticket);
    }
  },
};
