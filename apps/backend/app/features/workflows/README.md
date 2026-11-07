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
- 전환 실패 후 재시도: `cutover` 단계가 실패한 run 의 `resume` 은 승인 ②를 취소하고 `waiting_approval`/`cutover` 에서 멈춥니다. 새 점검을 본 사람이 다시 승인해야 합니다. 에이전트가 이미 전환을 시작했으면 요청을 409 로 거절하므로 두 번 전환되지 않고, workflows 는 결과만 확인합니다. 다른 단계의 재시도는 다시 승인받지 않습니다. 승인 이력(`approval_records`)은 지우지 않습니다.
- 전환 결과(`cutover_outcome`): 실패 직후 동작을 정합니다. `rolled_back`·`not_started` 는 원본이 처리 중임이 확인된 경우라 승인 ②를 바로 취소합니다. `unknown`(시간 초과, 연락 끊김)·`post_switch_failed`(전환 후 오류)는 트래픽이 넘어갔을 수 있어 승인을 남깁니다. 사람이 확인한 뒤 수동 입력으로 완료를 기록할 수 있고, 화면은 "수동 확인 필요" 를 계속 보여 줍니다. 값은 요청 전에 `unknown` 으로 바꾸므로 도중에 끊겨도 원본에 있다고 말하지 않습니다. 앞선 시도의 `unknown`·`post_switch_failed` 는 다음 시도가 시작되지 못해도 지우지 않습니다.

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
| POST | `/runs` | 이전 요청(`MigrationRequest`)으로 run 생성 후 백그라운드로 진행 (201). `github_repo_url` 은 선택이고, `service_name` 이 있으면 비어 있는 `ecs_service` 를 채웁니다. 어댑터가 없는 목적지는 `422` |
| GET | `/runs` | 최근 run 목록 (`limit` 1~200) |
| GET | `/runs/{id}` | run 상태 (`RunDetail`: `RunStatus` + 승인 ② 대기 중일 때 `cutover_readiness`, CDC 작업 ID `cdc_job_id`) |
| POST | `/runs/{id}/approve` | `{"gate": "plan" \| "cutover", "comment": ..., "approver": ..., "acknowledge_unknown": false, "preflight_verdict": ..., "preflight_measured_at": ...}` 승인 후 진행. 승인 ②는 아래 판정을 통과해야 합니다. `approver` 는 100자 이내, 공백만 있으면 없는 값. 네트워크 호출을 하지 않습니다 |
| GET | `/runs/{id}/preflight` | 실시간 전환 전 점검 (`PreflightReport`). 참고용입니다. `waiting_approval`/`cutover` 가 아니면 `409` |
| POST | `/runs/{id}/abort` | 중단 (`{"reason": ...}` 선택). 이어서 백그라운드로 복제 자원을 정리합니다 |
| POST | `/runs/{id}/cleanup` | 중단 뒤 정리에 실패한 run 의 복제 자원 정리를 다시 시도 (`cleanup_state` 가 `failed` 일 때만) |
| POST | `/runs/{id}/resume` | 실패한 단계 재시도·끊긴 진행 재개 |
| POST | `/runs/{id}/stages/{stage}/manual` | 다음 차례 단계 결과를 수동 입력 (`{"output": {...}}`) |
| GET·POST | `/runs/{id}/diagnosis`, `/runs/{id}/diagnose` | 실패한 run 의 원인·복구 조치. 실패가 아니면 `409` |

상태가 맞지 않는 명령은 `409`, 없는 run은 `404`, 잘못된 입력은 `422`입니다.

## 목적지 어댑터 (`target_adapters.py`)
provisioning 과 app_deploy 는 요청의 `target_infra.provider` 로 고른 어댑터를 부릅니다. 지금은 `gcp` 만 등록돼 있고 `target_infra` 가 없는 요청도 `gcp` 입니다. 새 클라우드의 계약과 아직 GCP 에 묶인 부분은 `docs/TARGET_ADAPTER_SPEC.md` 에 있습니다.

## 승인 ② 판정
`readiness.py` 가 복제 상태, 초기 복사, 복제 지연, 행 수 불일치 테이블, 목적지 앱 상태를 봅니다. 화면과 API 가 같은 판정을 씁니다.

승인 ② 화면에는 두 가지 결과가 함께 나옵니다.

- **승인 조건** (`readiness.py`, 위 판정): 저장된 지표로 계산하고 승인을 막습니다. 네트워크를 쓰지 않아 승인 요청이 빠릅니다.
- **실시간 점검** (`preflight.py`, `GET /preflight`): 에이전트·목적지 앱·Worker 에 직접 물어 지금 상태를 보여 줍니다. 참고용이며 승인을 막지 않습니다. 저장된 값이 놓친 변화(예: 배포 후 앱 장애)를 사람이 보게 하려는 것이고, 두 결과는 다를 수 있습니다.
- 승인 기록(`approval_records`): `readiness_decision` 은 서버가 계산한 값이고, `approver`·`preflight_verdict`·`preflight_measured_at` 은 클라이언트가 보고한 값입니다.

- 실패 항목이 있으면 `409` 로 거부합니다. `acknowledge_unknown` 으로도 넘어가지 못합니다.
- 미제공 항목만 있으면 `acknowledge_unknown: true` 일 때만 승인합니다.
- 복제 지연 기준값(1MiB)은 임시값입니다. 팀 합의 후 바꿉니다.
- 초기 복사·행 수 불일치 테이블: 쓰기 중에는 두 DB 를 다른 순간에 읽어 값이 오가므로, 에이전트가 `cutover_ready` 로 올린 뒤의 순간값으로는 막지 않습니다. 이 값은 행 수만 비교합니다. 내용 비교는 전환 시점 스냅샷으로 트래픽을 푼 뒤에 합니다 (`verification`).
- CDC 설정 사전 점검: `provisioning` runner 는 목적지를 만들기 전에 `CDC_AGENT_URL` 과 OCI 설정(`vpn.OciSettings`)을 봅니다. 값이 없으면 변수 이름을 남기고 실패합니다. 시뮬레이션은 보지 않습니다. 클러스터 상태와 대역 겹침은 VPN 을 만들기 전에 `db_migration` 이 봅니다.
- `cdc_error`: CDC 추적이 streaming 이후 끝나면 `db_migration` 단계 지표에 남아 승인을 막습니다. 전환이 끝나 돌아온 경우는 정상 종료라 남기지 않습니다. 끝난 단계는 다시 열지 않고 지표만 갱신합니다.

## 중단과 복제 자원 정리
- run 을 중단하면 `service.stop` 으로 커넥터, 복제 슬롯, VPN 을 정리합니다. CDC 를 수동 입력으로 넘겼거나 아직 시작하지 않았으면 할 일이 없습니다.
- 진행 상태는 `cleanup_state`(`running`·`done`·`failed`)와 `cleanup_message` 에 남습니다. 복제 슬롯이 남으면 원본에 WAL 이 쌓이므로 `failed` 는 반드시 다시 시도하거나 직접 처리합니다.
- 전환을 시작한 작업은 자동으로 정리하지 않습니다(`failed`). 목적지에 새 쓰기가 있을 수 있어 트래픽과 데이터를 사람이 확인해야 합니다.
- CDC 추적 스레드는 "다음 `ctx.report()` 때 멈춤" 의 예외입니다. 중단을 봐도 스스로 정리하지 않고 추적을 이어 갑니다. 정리는 `clean_up_cdc` 한 곳에서만 합니다. 정리가 작업을 멈추면 추적이 `aborted` 를 보고 VPN 을 지우고, 정리가 거부되면 복제와 VPN 이 그대로 남습니다.

## 화면 지표와 시뮬레이션
- `secret_encryption`: 요청의 자격 증명 참조가 모두 `secret://` 이고 암호화 저장소에 실제로 있을 때만 `ENCRYPTED`. 확인할 수 없으면 "미제공" 입니다.
- `cloud_sql_private_ip`: provisioning 이 돌려준 주소가 사설 IP 면 `SECURE`, 아니면 `PUBLIC`.
- `serving`: 에이전트의 `traffic_mode` 에서 옮깁니다.
- `cutover_seconds`·`cutover_seconds_source`: 팀 합의(박준서, 11/5)대로 Worker 의 `released_at - hold_started_at`(에이전트 `evidence.cutover.t0`·`t1`)을 씁니다 (`worker`). 두 값을 쓸 수 없으면 에이전트 `cutover_ms` 를 씁니다 (`agent`). 둘 다 없으면 "미제공" 입니다.
- `cutover_steps`: 에이전트 `evidence.cutover.steps` 의 `step`·`elapsed_ms`·`waited_ms`·`mismatch`·`error`(접속 정보를 가린 뒤 300자까지)만 옮깁니다. 진행 중과 실패 직전에도 보고합니다.
- `cutover_sequence`·`cutover_sequence_missing`: 성공 시 순서 검증 결과(`VERIFIED`/`INCOMPLETE`)와 빠진 단계입니다. 세 번째 단계는 `snapshot_fixed` 이고, 이전 에이전트의 `validated` 도 인정합니다. 증적이 없으면(시뮬레이터 포함) 단계 지표는 모두 "미제공" 입니다.
- `verification`: 전환 시점 스냅샷 비교 결과입니다. 전환 시간은 트래픽을 푼 시점에 확정하고, 단계는 이 값이 정해질 때까지 기다립니다.
  - `running`(비교 중), `match`(일치), `mismatch`(불일치), `unverifiable`(비교를 끝내지 못함)은 에이전트 값이고, `unknown`(기다리는 동안 결과를 못 받음)은 workflows 값입니다. 보고가 없는 에이전트는 기다리지 않고 "미제공" 입니다.
  - `mismatch` 는 단계를 실패로 남기고 `cutover_outcome` 을 `post_switch_failed` 로 바꿉니다. `unverifiable`·`unknown` 은 데이터가 다르다는 뜻이 아니어서 경고만 남기고 단계는 성공합니다.
- `cutover_outcome`: 처음 계약에 없던 값으로, 실패 직후 동작과 화면의 복귀 안내를 정하려고 workflows 가 추가했습니다. `not_started`(전환이 시작되지 않음), `rolled_back`(원본으로 복귀함), `unknown`(결과 불명), `post_switch_failed`(경로를 바꾼 뒤 오류), `switched`(전환 완료) 중 하나입니다.
  - `rolled_back` 은 에이전트 보고만으로 쓰지 않습니다. 복귀 단계에 실패가 없고, Worker 상태가 `mode`·`next_origin` 모두 `SOURCE` 일 때만 씁니다. `WORKER_URL` 이 없거나 읽지 못하면 `unknown` 입니다. 전환 요청 응답을 잃은 경우도 `unknown` 입니다.
  - 결과를 모르는데 트래픽이 원본이라고 확인되지 않으면 중단 뒤 복제 자원을 자동으로 정리하지 않습니다.
  - 불확실한 결과로 승인 ② 대기에 돌아오면, 승인 조건이 막아도 메모를 남겨 `cutover` 를 수동 입력으로 기록할 수 있습니다. 전환을 다시 요청하지 않습니다.
- `failed_requests`·`probe_*`: `ALPHA_PROBE_URL`(Worker 공개 주소)이 있으면 전환 중 프로버가 GET 으로 실측합니다. 응답 코드 400 이상, 연결 실패, 시간 초과를 실패로 셉니다. 읽기만 잽니다. 주소가 없으면 "미제공" 입니다. 시뮬레이션에서는 프로버를 켜지 않으며, 시뮬레이터가 보낸 값이 그대로 보이고 화면에 시뮬레이션으로 표시합니다. 에이전트 loadcheck 결과를 이 값으로 보고할지는 미정입니다 (박준서).
- `write_requests`·`write_failed_requests`: `WORKER_URL` 이 있으면 전환 뒤 Worker 의 `request_counts` 에서 읽습니다. 보류 시작부터 끝난 실제 쓰기 요청을 세며, 원본 5xx·연결 실패가 실패입니다. 집계가 이번 보류의 것이 아니면(Worker 객체 재시작 등) 보고하지 않습니다.
- 에이전트 오류 문구: 단계 `error`, 전환 단계 메시지, `cdc_error` 는 저장 전에 주소·포트·사용자·비밀번호·토큰을 가리고 300자로 자릅니다. `measure_error` 는 예외 원문이고 화면이 쓰지 않아 cutover·db_migration 지표로 넘기지 않습니다. 실시간 점검은 이 값을 에이전트에서 직접 읽습니다.
- `iam_audit_status`: 보내는 모듈이 아직 없어 "미제공" 입니다. 그래서 `COMPLIANT` 배지도 켜지지 않습니다.
- 시뮬레이션: `CDC_AGENT_URL` 이 비어 있고 `ALPHA_CDC_SIMULATION=1` 일 때만 켜집니다. 실제 URL 이 있으면 실제 에이전트를 부릅니다. 실제 VPN 은 만들지 않고, 화면에 시뮬레이션으로 표시합니다.
- AI 진단: `ALPHA_AI_ENABLED=1` + `pip install -e ".[ai]"`. 없으면 규칙 기반으로 진단하고 화면에 그렇게 표시합니다.

## 전환 전 점검 (`preflight.py`)
승인 ② 전에 보는 **참고용** 점검입니다. 결과가 `no_go` 여도 승인을 막지 않습니다. 점검은 `GET /preflight` 에서만 하고, 승인 요청은 프록시 제한(30초) 안에 끝나도록 점검하지 않습니다.

`approval_records` 의 `preflight_verdict`·`preflight_measured_at` 은 승인 화면에 표시된 판정과 측정 시각을 **클라이언트가 보고한 값**입니다. 직접 적는 `approver` 처럼 서버가 검증하지 않으므로, 실제 상태의 증거로 쓰지 않습니다. 점검 결과가 없으면 `null` 이고, 승인 ① 에는 보내도 저장하지 않습니다.

| 점검 | 근거 | 결과 |
| --- | --- | --- |
| `job_ready` | 에이전트 job | `waiting_approval`/`cutover_ready` 면 `pass`, 아니면 `fail` |
| `db_connectivity` | `measure_error`·`measured_at` | 측정 오류면 `fail`(원문은 싣지 않음), 측정 시각이 시간대가 있는 ISO 문자열이면 `pass`, 아니면 `unknown`. 합의된 기준이 없어 시각의 나이는 보지 않습니다 |
| `data_match` | `mismatch`(행 수 비교) | 0 이면 `pass`, 0 보다 크면 `fail`. 측정 오류면 `unknown` |
| `replication_lag` | `replication_lag_bytes` | 기준이 없으면 `info`. `ALPHA_CUTOVER_MAX_LAG_BYTES` 가 있으면 `pass`/`fail`, 값이 잘못되면 `unknown` |
| `target_ready` | `app_deploy` 의 `app_url` + `/health/ready` | 200 이면 `pass`, 아니면 `fail`. 수동 입력 주소나 https 가 아닌 주소는 호출하지 않고 `unknown` |
| `worker_target` | `WorkerClient.get_target()` | `app_url` 과 같으면 `pass`, 저장된 주소가 다르면 `fail`. 저장된 주소가 없으면 Worker 가 설정된 기본 대상을 쓰는 것이라 `unknown`. Worker 미설정이면 `unknown` |

- 판정: `fail` 이 하나라도 있으면 `no_go`, 없고 `unknown` 이 있으면 `unverified`, 그 외는 `go` 입니다. `info` 는 판정에 영향이 없습니다. 시뮬레이션이면 항상 `unverified` 입니다.
- 에이전트에 연결하지 못하면 앞의 네 점검은 `unknown` 입니다. 예외 원문·호스트·주소는 결과와 로그에 남기지 않습니다.

## 전환 순서
- 에이전트가 `hold → marker_on_target → validated → switched_to_target → released` 순서를 실행합니다. workflows 는 `cutover` 를 한 번 호출하고 증적으로 순서를 검증만 합니다 (PR #45). 다시 구현하거나 Worker 를 직접 부르지 않습니다.
- 순서 사이에 다른 단계가 끼어도 됩니다. `INCOMPLETE` 는 경고만 남기고 단계를 실패로 두지 않습니다. 트래픽이 이미 넘어갔기 때문입니다.
- 에이전트에 보내는 `approved_by` 는 가장 최근 승인 ②의 `approver` 입니다. 없으면 `workflows` 입니다.

## 상태 저장
- 백엔드 DB의 상태 테이블 하나(`runs`)에 run 전체를 JSON으로 저장합니다. 서버를 재시작해도 남습니다.
- 지금은 표준 라이브러리 SQLite 파일입니다 (`WORKFLOWS_DB_PATH`, 기본 `apps/backend/.data/workflows.sqlite3`). 새 의존성 없음.
- 읽기·쓰기는 한 트랜잭션으로 처리하지만, 실행 중인 단계 관리는 프로세스 하나를 기준으로 합니다. **uvicorn worker 는 1개로 실행**하세요.

## 미정 사항 (합의 필요)
- `MigrationRequest` 필드: discovery `DiscoveryInput` 확정 후 맞춤 (김준석)
- 전환 전 복제 지연 기준: 합의된 값이 없습니다. 승인 조건은 임시 1MiB 로 막고, 실시간 점검은 `ALPHA_CUTOVER_MAX_LAG_BYTES` 가 없으면 참고값으로만 표시합니다 (안재현)
- `failed_requests` 를 에이전트 loadcheck 결과로 보고할지 (박준서)
- 전환(cutover) 진입점: MVP 는 에이전트가 순서를 실행합니다 (PR #45). ADR-004 는 MVP 이후 논의합니다 (안재현)
- 서버가 단계 실행 중 재시작되면 그 단계는 `running`으로 남습니다. 자동 복구 방식은 미정이며, 지금은 확인 후 abort 합니다.
- 상태 DB를 SQLite 파일로 둘지, 다른 DB로 옮길지
