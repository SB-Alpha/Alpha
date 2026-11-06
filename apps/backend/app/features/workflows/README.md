# workflows

## 책임
각 모듈의 입력·출력을 연결해 전체 이전 흐름을 실행합니다. 모듈의 기술 구현은 여기서 다시 만들지 않습니다.

- run 생성, 단계 순서 호출, 승인 대기, 상태 저장, 중단, 수동 처리 입력, frontend용 API

## 단계와 승인 지점

```
discovery → planning → [승인 ① plan] → provisioning → db_migration → app_deploy → [승인 ② cutover] → cutover
```

- `discovery` 는 assessment 에 통합돼(11/4, PR #27) 조회 없이 바로 통과합니다 (`merged_into: planning`). 원본 조회 결과는 planning 출력의 `source` 에 있습니다.

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
| GET | `/runs/{id}` | run 상태 (`RunDetail`: `RunStatus` + 승인 ② 대기 중일 때 `cutover_readiness`) |
| POST | `/runs/{id}/approve` | `{"gate": "plan" \| "cutover", "acknowledge_unknown": false}` 승인 후 진행. 승인 ②는 아래 판정을 통과해야 합니다 |
| POST | `/runs/{id}/abort` | 중단 (`{"reason": ...}` 선택). 이어서 백그라운드로 복제 자원을 정리합니다 |
| POST | `/runs/{id}/cleanup` | 중단 뒤 정리에 실패한 run 의 복제 자원 정리를 다시 시도 (`cleanup_state` 가 `failed` 일 때만) |
| POST | `/runs/{id}/resume` | 실패한 단계 재시도·끊긴 진행 재개 |
| POST | `/runs/{id}/stages/{stage}/manual` | 다음 차례 단계 결과를 수동 입력 (`{"output": {...}}`) |
| GET·POST | `/runs/{id}/diagnosis`, `/runs/{id}/diagnose` | 실패한 run 의 원인·복구 조치. 실패가 아니면 `409` |

상태가 맞지 않는 명령은 `409`, 없는 run은 `404`, 잘못된 입력은 `422`입니다.

## 승인 ② 판정
`readiness.py` 가 복제 상태, 초기 복사, 복제 지연, 불일치 테이블, 목적지 앱 상태를 봅니다. 화면과 API 가 같은 판정을 씁니다.

- 실패 항목이 있으면 `409` 로 거부합니다. `acknowledge_unknown` 으로도 넘어가지 못합니다.
- 미제공 항목만 있으면 `acknowledge_unknown: true` 일 때만 승인합니다.
- 복제 지연 기준값(1MiB)은 임시값입니다. 팀 합의 후 바꿉니다.
- 초기 복사·불일치 테이블: 쓰기 중에는 두 DB 를 다른 순간에 읽어 값이 오가므로, 에이전트가 `cutover_ready` 로 올린 뒤의 순간값으로는 막지 않습니다. 쓰기를 멈춘 전환 기준점에서 다시 검사해 다르면 전환하지 않습니다.
- `cdc_error`: CDC 추적이 streaming 이후 끝나면 `db_migration` 단계 지표에 남아 승인을 막습니다. 끝난 단계는 다시 열지 않고 지표만 갱신합니다.

## 중단과 복제 자원 정리
- run 을 중단하면 `service.stop` 으로 커넥터, 복제 슬롯, VPN 을 정리합니다. CDC 를 수동 입력으로 넘겼거나 아직 시작하지 않았으면 할 일이 없습니다.
- 진행 상태는 `cleanup_state`(`running`·`done`·`failed`)와 `cleanup_message` 에 남습니다. 복제 슬롯이 남으면 원본에 WAL 이 쌓이므로 `failed` 는 반드시 다시 시도하거나 직접 처리합니다.
- 전환을 시작한 작업은 자동으로 정리하지 않습니다(`failed`). 목적지에 새 쓰기가 있을 수 있어 트래픽과 데이터를 사람이 확인해야 합니다.

## 화면 지표와 시뮬레이션
- `secret_encryption`: 요청의 자격 증명 참조가 모두 `secret://` 이고 암호화 저장소에 실제로 있을 때만 `ENCRYPTED`. 확인할 수 없으면 "미제공" 입니다.
- `cloud_sql_private_ip`: provisioning 이 돌려준 주소가 사설 IP 면 `SECURE`, 아니면 `PUBLIC`.
- `serving`·`cutover_seconds`: 에이전트의 `traffic_mode`·`cutover_ms` 에서 옮깁니다.
- `failed_requests`·`probe_*`: `ALPHA_PROBE_URL`(Worker 공개 주소)이 있으면 전환 중 프로버가 GET 으로 실측합니다. 응답 코드 400 이상, 연결 실패, 시간 초과를 실패로 셉니다. 읽기만 재며, 주소가 없으면 "미제공" 입니다.
- `write_requests`·`write_failed_requests`: `WORKER_URL` 이 있으면 전환 뒤 Worker 의 `request_counts` 에서 읽습니다. 보류 시작부터 끝난 실제 쓰기 요청을 세며, 원본 5xx·연결 실패가 실패입니다. 집계가 이번 보류의 것이 아니면(Worker 객체 재시작 등) 보고하지 않습니다.
- `iam_audit_status`: 보내는 모듈이 아직 없어 "미제공" 입니다. 그래서 `COMPLIANT` 배지도 켜지지 않습니다.
- 시뮬레이션: `CDC_AGENT_URL` 이 비어 있고 `ALPHA_CDC_SIMULATION=1` 일 때만 켜집니다. 실제 URL 이 있으면 실제 에이전트를 부릅니다. 실제 VPN 은 만들지 않고, 화면에 시뮬레이션으로 표시합니다.
- AI 진단: `ALPHA_AI_ENABLED=1` + `pip install -e ".[ai]"`. 없으면 규칙 기반으로 진단하고 화면에 그렇게 표시합니다.

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
