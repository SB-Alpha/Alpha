# Worker 제어 API 계약

CDC 에이전트(`cdc_agent/worker.py`)가 쓰기 전환 때 부르는 Cloudflare Worker 제어 API입니다. 트래픽 전환 문서 3절 제안을 실제 호출 형식으로 고정한 것입니다. 로컬 시험용 구현은 `../local/fake_worker.py`입니다.

## 공통

- 주소: `WORKER_URL` (예: `https://<서비스 도메인>`). 아래 경로는 그 뒤에 붙습니다.
- 인증: 모든 `/control/*` 요청에 `Authorization: Bearer <제어 토큰>`. 틀리면 `401`.
- 본문·응답: JSON. 시각은 UTC ISO 8601 밀리초 (`2026-11-04T23:10:00.120Z`).
- 오류: `4xx`/`5xx`와 `{"error": "<사유>"}`. 상태 조건이 안 맞으면 `409`.
- 에이전트 타임아웃: 요청당 5초.

## 모드

| `mode` | 사용자 요청 처리 |
| --- | --- |
| `SOURCE` | AWS로 전달 |
| `HOLD` | 붙잡아 둔다. 실패로 돌려보내지 않는다 |
| `TARGET` | GCP로 전달 |

`next_origin`은 보류가 풀릴 때 보낼 곳입니다 (`SOURCE` 또는 `TARGET`).

## 상태 객체

네 API가 모두 이 객체를 돌려줍니다.

```json
{
  "mode": "HOLD",
  "next_origin": "SOURCE",
  "run_id": "job01",
  "hold_started_at": "2026-11-04T23:10:00.120Z",
  "released_at": null,
  "held_count": 3,
  "inflight_source": 0,
  "updated_at": "2026-11-04T23:10:00.410Z",
  "request_counts": {
    "since": "2026-11-04T23:10:00.120Z",
    "write_total": 12,
    "write_failed": 0,
    "read_total": 40,
    "read_failed": 0
  }
}
```

| 필드 | 뜻 |
| --- | --- |
| `held_count` | 이번 보류 동안 붙잡은 요청 수 (보류 시작 때 0) |
| `inflight_source` | AWS로 이미 넘겼는데 아직 응답이 안 온 요청 수 (모드와 상관없이 항상 집계) |
| `hold_started_at`, `released_at` | 에이전트가 T0·T1로 기록 |
| `request_counts` | 전환 관측용 집계 (에이전트는 쓰지 않는다). GET·HEAD·OPTIONS는 읽기, 그 밖은 쓰기. 끝난 요청을 세고, 원본 응답 5xx와 원본 연결 실패를 실패로 본다. 새 보류 때 0으로 시작한다 |
| `request_counts.since` | 집계 시작 시각. 새 보류면 `hold_started_at`과 같다. 저장하지 않으므로 Worker 객체가 다시 뜨면 달라지고, 이때 호출 쪽은 이 값을 믿지 않는다 |

## API

### `POST /control/hold`

```json
{"run_id": "job01", "max_hold_ms": 3000}
```

- `SOURCE`일 때 → `mode: HOLD`, `next_origin: SOURCE`, `held_count: 0`으로 바꾸고 상태 반환.
- 이미 같은 `run_id`로 `HOLD`면 → 아무것도 바꾸지 않고 상태 반환 (재시도).
- 그 밖 (`TARGET`, 다른 `run_id`의 `HOLD`) → `409`.

### `POST /control/switch`

```json
{"run_id": "job01", "to": "TARGET"}
```

- 같은 `run_id`로 `HOLD`일 때 → `next_origin`을 `to`로 바꾸고 상태 반환. 다시 불러도 결과가 같다.
- 그 밖 → `409`.

### `POST /control/release`

```json
{"run_id": "job01"}
```

- 같은 `run_id`로 `HOLD`일 때 → `mode`를 `next_origin`으로 바꾸고, 붙잡은 요청을 그쪽으로 보낸 뒤 상태 반환 (`released_at`, `held_count` 포함).
- 이미 풀려 있으면 → 아무것도 바꾸지 않고 상태 반환 (재시도).

### `GET /control/status`

현재 상태 반환. 언제나 `200`. 에이전트는 보류 직후 `inflight_source`가 0이 될 때까지 20ms 간격으로 부릅니다 (최대 2초).

## 지켜야 할 동작

1. **보류 중 요청은 실패시키지 않는다.** 해제될 때까지 기다렸다가 `next_origin`으로 보낸다.
2. **모드 변경은 즉시 반영된다.** `hold` 응답을 돌려준 뒤에 들어온 요청은 하나도 AWS로 가면 안 된다 (Durable Objects 제안).
3. **`inflight_source`는 정확해야 한다.** 에이전트는 이 값이 0이 되면 원본 쓰기를 막는다. 응답이 오기 전 요청을 빼먹으면 그 요청이 막힌 원본에서 실패한다.
4. **보류 상한 안전장치.** 정상이라면 에이전트가 `max_hold_ms` 안에 `release`를 부른다. 에이전트가 응답하지 않아 `max_hold_ms + 10초`가 지나면 Worker가 스스로 `next_origin`으로 해제한다 (제안. 트래픽 문서 6절에서 확정).

## 에이전트가 부르는 순서

```text
hold → status(반복, inflight_source=0) → [원본 쓰기 차단 · marker 확인 · 검증]
     → switch(to=TARGET) → release                      정상: T1−T0 ≤ 3초
     → release (switch 없이)                             중간 실패: 원본으로 재개
```
