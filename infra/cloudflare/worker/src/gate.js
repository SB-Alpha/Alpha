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
  arbitrage: {
    enabled: false,
    read_target_weight: 0.0,
    max_lag_bytes: 1024,
    current_lag_bytes: null, // 초기값 null (알 수 없음 -> Fail-Closed 적용)
    lag_updated_at: null,
    read_after_write_fence_sec: 5,
  },
  quarantine: {
    isolated_nodes: [],
    reroute_target: null,
    quarantined_at: null,
  },
});

// 요청 집계. 저장하지 않으므로 객체가 다시 뜨면 since 가 새 시각이 되어 호출 쪽이 알아챌 수 있다.
const newCounts = (since = iso()) => ({ since, write_total: 0, write_failed: 0, read_total: 0, read_failed: 0 });

const ok = (body) => ({ status: 200, body });
const conflict = (message) => ({ status: 409, body: { error: message } }); 

export class Gate extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.state = initialState(); 
    this.waiters = [];
    this.tickets = new Map();
    this.seq = 0;
    this.counts = newCounts();
    this.recentWrites = new Map();

  
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
    return { ...visible, inflight_source: inflightSource, request_counts: { ...this.counts } };
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
  async enter({ isWrite = false, lagBytes = null, clientIp = null } = {}) {
    await this.expireHold();

    if (isWrite && clientIp) {
      this.recentWrites.set(clientIp, Date.now());
    }

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

    let origin = this.state.mode;
    let viaArbitrage = false;
    let viaQuarantine = false;
    let isBlocked = false;
    let blockReason = null;

    // 보안 침해 격리(Quarantine) 라우팅:
    // SOURCE 모드이고 격리 명단에 유효한 노드가 등록되어 있을 때
    // 참고: 현재 게이트 아키텍처에서 노드 격리는 해당 워크로드 원본(SOURCE) 전체에 대한
    // 안전 격리(쓰기 503 차단 및 TARGET 읽기 우회)로 동작합니다.
    const q = this.state.quarantine;
    const cleanNodes = Array.isArray(q?.isolated_nodes)
      ? q.isolated_nodes.filter((n) => typeof n === "string" && n.trim().length > 0).map((n) => n.trim())
      : [];
    const hasQuarantine = this.state.mode === "SOURCE" && cleanNodes.length > 0;

    if (hasQuarantine) {
      if (isWrite) {
        // [안전 게이트]: 전환 전 원본 침해 격리 시 쓰기(Write)를 Read-Only 타깃으로 보내면 split-brain 및 데이터 유실 발생.
        // 쓰기 요청은 503 Service Unavailable ("quarantine_write_blocked") 로 안전하게 차단한다.
        isBlocked = true;
        blockReason = "quarantine_write_blocked";
        viaQuarantine = true;
      } else {
        // 읽기(Read) 요청만 안전한 대체 타깃으로 즉시 우회
        origin = "TARGET";
        viaQuarantine = true;
      }
    } else {
      // 차익거래 라우팅: 모드가 SOURCE일 때, 쓰기가 아닌 읽기 요청이고
      // 복제 지연이 허용 임계치 이내이면 Cloud Run(TARGET)으로 가중치 기반 라우팅
      const arb = this.state.arbitrage;
      if (this.state.mode === "SOURCE" && arb?.enabled && !isWrite) {
        // 복제 지연 세이프가드: 제어 평면(CDC)의 측정 지연과 클라이언트 헤더 중 보수적인(큰) 값 선택
        // 지연 메트릭이 15초 이상 갱신되지 않았으면 지연 상태를 모르는 것(stale -> null)으로 판단
        const LAG_STALE_MS = 15000;
        const isStale = !arb.lag_updated_at || (Date.now() - new Date(arb.lag_updated_at).getTime() > LAG_STALE_MS);
        const controlLag = isStale ? null : (arb.current_lag_bytes ?? null);
        const clientLag = lagBytes !== null && !isNaN(lagBytes) ? lagBytes : null;
        const effectiveLag = clientLag !== null && controlLag !== null ? Math.max(controlLag, clientLag) : (controlLag ?? clientLag);
        const maxLag = arb.max_lag_bytes ?? 1024;

        // 1. Read-After-Write 세션 고정 세이프가드: 최근 쓰기를 수행한 클라이언트(IP)는 N초간 마스터(SOURCE)로 고정
        const fenceSec = Number(arb.read_after_write_fence_sec) || 5;
        const lastWrite = clientIp ? this.recentWrites.get(clientIp) : null;
        const isRecentlyWritten = Boolean(lastWrite && Date.now() - lastWrite < fenceSec * 1000);

        // Fail-closed: 최근 쓰기 세션이거나, 지연 상태를 알 수 없거나 임계치를 초과하면 절대 우회하지 않고 SOURCE(마스터)로 펜싱
        const safe = !isRecentlyWritten && effectiveLag !== null && effectiveLag <= maxLag;
        if (safe && Math.random() < (arb.read_target_weight ?? 0.0)) {
          origin = "TARGET";
          viaArbitrage = true;
        }
      }
    }

    if (isBlocked) {
      return {
        origin: "BLOCKED",
        ticket: null,
        target: null,
        viaArbitrage: false,
        viaQuarantine: true,
        isBlocked: true,
        blockReason,
      };
    }

    const ticket = ++this.seq;
    this.tickets.set(ticket, { origin, startedAt: Date.now() });
    // target: 제어 명령으로 정한 GCP 쪽 주소 (없으면 null).
    // 격리 우회 시 reroute_target 이 올바른 URL 프로토콜(http/https)이면 우선 사용합니다.
    const effectiveTarget = (viaQuarantine && this.state.quarantine?.reroute_target && (this.state.quarantine.reroute_target.startsWith("http://") || this.state.quarantine.reroute_target.startsWith("https://")))
      ? this.state.quarantine.reroute_target
      : (this.state.target_url ?? null);

    return {
      origin,
      ticket,
      target: effectiveTarget,
      viaArbitrage,
      viaQuarantine,
      isBlocked: false,
      blockReason: null,
    };
  }

  // exit: 요청이 원본에서 응답을 받아 끝났을 때 부른다. "처리 중" 명단에서 번호표를 지우고 결과를 센다.
  exit(ticket, { isWrite, isFailure }) {
    this.tickets.delete(ticket);
    const kind = isWrite ? "write" : "read";
    this.counts[`${kind}_total`] += 1;
    if (isFailure) this.counts[`${kind}_failed`] += 1;
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
    this.counts = newCounts(now);
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

  // 동적 차익거래(Arbitrage) 정책 설정 및 조회
  async setArbitrage(body = {}) {
    await this.expireHold();
    const enabled = typeof body.enabled === "boolean" ? body.enabled : body.enabled === "true";
    const readWeight = Math.max(0.0, Math.min(1.0, Number(body.read_target_weight) || 0.0));
    const maxLag = Number.isFinite(Number(body.max_lag_bytes)) ? Math.max(0, Number(body.max_lag_bytes)) : 1024;
    const fenceSec = Number.isFinite(Number(body.read_after_write_fence_sec))
      ? Math.max(0, Number(body.read_after_write_fence_sec))
      : (this.state.arbitrage?.read_after_write_fence_sec ?? 5);
    const hasLag = Number.isFinite(Number(body.current_lag_bytes));
    const currentLag = hasLag
      ? Math.max(0, Number(body.current_lag_bytes))
      : (body.current_lag_bytes === null ? null : (this.state.arbitrage?.current_lag_bytes ?? null));
    const lagUpdatedAt = hasLag ? iso() : (this.state.arbitrage?.lag_updated_at ?? null);

    this.state.arbitrage = {
      enabled,
      read_target_weight: readWeight,
      max_lag_bytes: maxLag,
      current_lag_bytes: currentLag,
      lag_updated_at: lagUpdatedAt,
      read_after_write_fence_sec: fenceSec,
    };
    this.state.updated_at = iso();
    await this.save();
    return ok(this.state.arbitrage);
  }

  async getArbitrage() {
    return ok(
      this.state.arbitrage ?? {
        enabled: false,
        read_target_weight: 0.0,
        max_lag_bytes: 1024,
        current_lag_bytes: null,
        lag_updated_at: null,
        read_after_write_fence_sec: 5,
      }
    );
  }

  async setQuarantine({ isolated_nodes, reroute_target } = {}) {
    await this.expireHold();
    // 노드 ID 엄격 검증: string 타입이고 비어있지 않은 값만 필터링 (null, number, 빈문자열 등 거절)
    const validNodes = Array.isArray(isolated_nodes)
      ? isolated_nodes.filter((n) => typeof n === "string" && n.trim().length > 0).map((n) => n.trim())
      : [];
    const cleanTarget = typeof reroute_target === "string" && reroute_target.trim().length > 0
      ? reroute_target.trim()
      : null;

    this.state.quarantine = {
      isolated_nodes: validNodes,
      reroute_target: cleanTarget,
      quarantined_at: validNodes.length > 0 ? iso() : null,
    };
    this.state.updated_at = iso();
    await this.save();
    return ok(this.state.quarantine);
  }

  async getQuarantine() {
    return ok(
      this.state.quarantine ?? {
        isolated_nodes: [],
        reroute_target: null,
        quarantined_at: null,
      }
    );
  }

// reset: 규격에는 없는 보조 기능. 처음 상태(SOURCE)로 되돌림
// 테스트 여러 번 반복할 때 TARGET 에서 다시 SOURCE 로 돌아가려고 만들었다.
  async reset() {
    this.state = initialState();
    this.tickets.clear();
    this.counts = newCounts();
    this.recentWrites.clear();
    await this.save();
    for (const wake of this.waiters.splice(0)) wake();
    return ok(this.view());
  }
}
