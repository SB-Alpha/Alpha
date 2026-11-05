# workflows

## 책임
각 모듈의 입력·출력을 연결해 전체 이전 흐름을 실행합니다. 모듈의 기술 구현은 여기서 다시 만들지 않습니다.

- run 생성, 단계 순서 호출, 승인 대기, 상태 저장, 중단, 수동 처리 입력, frontend용 API

## 단계와 승인 지점

```
discovery → planning → [승인 ① plan] → provisioning → db_migration → app_deploy → [승인 ② cutover] → cutover
```

| 상황 | run `state` | 단계 `state` |
| --- | --- | --- |
| 단계 실행 중 | `running` | `running` (모듈이 `ctx.report()`로 갱신) |
| 승인 지점 도착 | `waiting_approval` (`waiting_gate`=`plan`/`cutover`) | 다음 단계는 `pending` |
| 모듈 미연결 | `paused` ("미구현 — 수동 입력 대기") | `pending` (가짜 성공 없음) |
| 모듈 예외 | `failed` | `failed` (오류 메시지 기록) |
| 운영자 중단 | `aborted` | 실행 중이던 단계 `aborted` |
| 모든 단계 끝 | `succeeded` | 모두 `succeeded` |

- 수동 입력: run 이 `paused`·`failed` 일 때 다음 차례 단계의 결과(fixture)를 사람이 넣으면 그 단계는 `succeeded` + `manual=true`로 기록되고 이어서 진행합니다.
- 중단: run은 즉시 `aborted`가 됩니다. 실행 중인 모듈은 다음 `ctx.report()` 호출 때 멈춥니다.
- 재개: `failed` 단계를 `pending`으로 돌리고 다시 실행합니다 (`resume`).

## 모듈 연결 계약 (`schemas.py`)

```py
def run(inp: <Module>Input, ctx: RunContext) -> <Module>Output: ...
# 실행 중 ctx.report(state, progress, metrics, message) 로 보고
```

- 다른 모듈은 `app.features.workflows.schemas`의 `RunContext`만 import합니다.
- `report()`의 `state`는 진행 중 상태(`running`·`waiting_approval`·`paused`)만 받습니다. 단계의 끝(`succeeded`·`failed`)은 `run()`이 값을 돌려주거나 예외를 내는 것으로 정해집니다. `progress`는 0~100. 잘못된 값은 예외입니다.
- `metrics`는 단계별로 합쳐서 저장합니다. 값이 없으면 화면에 "미제공"으로 표시합니다.
- 모듈 연결은 `router.get_engine()`의 `runners`에 단계별 함수를 등록합니다. 함수는 `run.request`와 앞 단계 `run.outputs`로 그 모듈의 Input을 만들어 `run()`을 호출합니다.

## API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| POST | `/runs` | 이전 요청(`MigrationRequest`)으로 run 생성 후 백그라운드로 진행 (201) |
| GET | `/runs` | 최근 run 목록 (`limit` 1~200) |
| GET | `/runs/{id}` | run 상태 (`RunStatus`) |
| POST | `/runs/{id}/approve` | `{"gate": "plan" \| "cutover"}` 승인 후 진행 |
| POST | `/runs/{id}/abort` | 중단 (`{"reason": ...}` 선택) |
| POST | `/runs/{id}/resume` | 실패한 단계 재시도·끊긴 진행 재개 |
| POST | `/runs/{id}/stages/{stage}/manual` | 다음 차례 단계 결과를 수동 입력 (`{"output": {...}}`) |

상태가 맞지 않는 명령은 `409`, 없는 run은 `404`, 잘못된 입력은 `422`입니다.

## 상태 저장
- 백엔드 DB의 상태 테이블 하나(`runs`)에 run 전체를 JSON으로 저장합니다. 서버를 재시작해도 남습니다.
- 지금은 표준 라이브러리 SQLite 파일입니다 (`WORKFLOWS_DB_PATH`, 기본 `apps/backend/.data/workflows.sqlite3`). 새 의존성 없음.
- 읽기·쓰기는 한 트랜잭션으로 처리하지만, 실행 중인 단계 관리는 프로세스 하나를 기준으로 합니다. **uvicorn worker 는 1개로 실행**하세요.

## 미정 사항 (합의 필요)
- `MigrationRequest` 필드: discovery `DiscoveryInput` 확정 후 맞춤 (김준석)
- 계획이 `approvable=false`일 때 승인 ①을 막을지 (이주호·frontend)
- 전환(cutover) 단계 진입점·상태 조회 형태 (박준서, ADR-004 A안)
- 서버가 단계 실행 중 재시작되면 그 단계는 `running`으로 남습니다. 자동 복구 방식은 미정이며, 지금은 확인 후 abort 합니다.
- 상태 DB를 SQLite 파일로 둘지, 다른 DB로 옮길지
