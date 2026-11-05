// gate.js: 지금 사용자 요청을 어디로 보낼지 기억하고, 요청을 붙잡았다 놓는 부분
import { DurableObject } from "cloudflare:workers";

const DEFAULT_MAX_HOLD_MS = 3_000;
const GRACE_MS = 10_000;
const TICKET_TTL_MS = 60_000;

const iso = (ms = Date.now()) => new Date(ms).toISOString();

const initialState = () => ({
  mode: "SOURCE",
  next_origin: "SOURCE",
  run_id: null,
  hold_started_at: null,
  released_at: null, 
  held_count: 0,
  updated_at: null,
  max_hold_ms: DEFAULT_MAX_HOLD_MS,
  target_url: null, // 제어 명령(target)으로 정한 GCP 쪽 원본 주소. null 이면 설정값 UPSTREAM_TARGET 을 쓴다
});

const ok = (body) => ({ status: 200, body });
const conflict = (message) => ({ status: 409, body: { error: message } }); 

export class Gate extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.state = initialState(); 
    this.waiters = [];
    this.tickets = new Map();
    this.seq = 0;

  
    ctx.blockConcurrencyWhile(async () => {
      this.state = (await ctx.storage.get("state")) ?? initialState();
    });
  }

  async save() {
    await this.ctx.storage.put("state", this.state);
  }

  holdDeadlineMs() {
    return Date.parse(this.state.hold_started_at) + this.state.max_hold_ms + GRACE_MS;
  }


  view() {
    const now = Date.now();
    for (const [id, ticket] of this.tickets) {
      if (now - ticket.startedAt > TICKET_TTL_MS) this.tickets.delete(id);
    }
    let inflightSource = 0;
    for (const ticket of this.tickets.values()) if (ticket.origin === "SOURCE") inflightSource++;
    // max_hold_ms, target_url 은 내부용이라 상태 객체(규격)에는 넣지 않는다
    const { max_hold_ms: _internal, target_url: _target, ...visible } = this.state;
    return { ...visible, inflight_source: inflightSource };
  }

  // doRelease: 보류를 푼다. 붙잡혀 있던 요청이 next_origin 으로 감.
  async doRelease() {
    const now = iso();
    this.state.mode = this.state.next_origin;
    this.state.released_at = now;
    this.state.updated_at = now;
    await this.save();
    for (const wake of this.waiters.splice(0)) wake(); // splice(0) = 목록을 비우면서 꺼냄
  }


  async expireHold() {
    if (this.state.mode === "HOLD" && Date.now() >= this.holdDeadlineMs()) await this.doRelease();
  }

  // enter: 요청이 들어왔을 때 부른다. "보류 중"이면 붙잡아 두고, 풀리면 어디로 갈지(origin)를 돌려줌.
  async enter() {
    await this.expireHold();

    if (this.state.mode === "HOLD") {
      this.state.held_count += 1;

      while (this.state.mode === "HOLD") {
        const remaining = this.holdDeadlineMs() - Date.now();
        if (remaining <= 0) {
          await this.doRelease();
          break;
        }
        await Promise.race([
          new Promise((resolve) => this.waiters.push(resolve)),
          new Promise((resolve) => setTimeout(resolve, remaining)),
        ]);
      }
    }

    const origin = this.state.mode;
    const ticket = ++this.seq;
    this.tickets.set(ticket, { origin, startedAt: Date.now() });
    // target: 제어 명령으로 정한 GCP 쪽 주소 (없으면 null). 예전에 저장된 상태에는 이 칸이 없어서 ?? 로 맞춘다
    return { origin, ticket, target: this.state.target_url ?? null };
  }

  // exit: 요청이 원본에서 응답을 받아 끝났을 때 부른다. "처리 중" 명단에서 번호표를 지운다.
  exit(ticket) {
    this.tickets.delete(ticket);
  }


// 제어 API

  async hold({ run_id, max_hold_ms }) {
    await this.expireHold();
    const s = this.state;
    if (s.mode === "HOLD" && s.run_id === run_id) return ok(this.view());
    if (s.mode !== "SOURCE") {
      return conflict(s.mode === "HOLD" ? `mode is HOLD for run ${s.run_id}` : `mode is ${s.mode}`);
    }
    const now = iso();
    Object.assign(s, {
      mode: "HOLD",
      next_origin: "SOURCE",
      run_id,
      hold_started_at: now,
      released_at: null,
      held_count: 0,
      updated_at: now,
      max_hold_ms,
    });
    await this.save();
    return ok(this.view());
  }

  async switchTo({ run_id, to }) {
    await this.expireHold();
    const s = this.state;
    if (s.mode !== "HOLD" || s.run_id !== run_id) return conflict(`mode is ${s.mode} for run ${s.run_id}`);
    s.next_origin = to;
    s.updated_at = iso();
    await this.save();
    return ok(this.view());
  }

  async release({ run_id }) {
    await this.expireHold();
    const s = this.state;
    if (s.mode !== "HOLD") return ok(this.view());
    if (s.run_id !== run_id) return conflict(`held by run ${s.run_id}`);
    await this.doRelease();
    return ok(this.view());
  }

  async status() {
    await this.expireHold();
    return ok(this.view());
  }

  // GCP 쪽 원본 주소를 바꿈. url 이 null 이면 설정값(UPSTREAM_TARGET)으로 되돌림.
  async setTarget({ url }) {
    await this.expireHold();
    if (this.state.mode === "TARGET") return conflict("mode is TARGET: the target url cannot change after the cutover");
    this.state.target_url = url;
    await this.save();
    return ok({ url });
  }

  async getTarget() {
    return ok({ url: this.state.target_url ?? null });
  }

// reset: 규격에는 없는 보조 기능. 처음 상태(SOURCE)로 되돌림
// 테스트 여러 번 반복할 때 TARGET 에서 다시 SOURCE 로 돌아가려고 만들었다.
  async reset() {
    this.state = initialState();
    this.tickets.clear();
    await this.save();
    for (const wake of this.waiters.splice(0)) wake();
    return ok(this.view());
  }
}
