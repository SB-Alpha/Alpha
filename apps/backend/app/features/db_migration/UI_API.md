# db_migration 화면 연결 안내

화면은 `/db-migration/*`만 호출하면 키 등록부터 전환, 정리까지 처리할 수 있습니다. 경로(AWS, GCP, Azure 6개 조합)마다 호출이 달라지지 않습니다. 필드 정의는 `/openapi.json`의 `MigrationRunStatus`, `RunMetrics`에 있습니다.

## 화면 흐름

1. `GET /clouds`로 키가 있는지 보고, 없으면 `required_keys`로 입력 폼을 그려 `PUT /clouds/{cloud}`로 저장합니다.
2. `POST /runs`로 시작하고 받은 `run_id`로 `GET /runs/{run_id}`를 주기적으로 읽습니다.
3. `database_choices`가 오면 선택 목록을 보여 주고 `POST /runs/{run_id}/retry`로 고른 값을 보냅니다.
4. `ready_for_cutover`가 `true`이면 승인 버튼을 열고 `POST /runs/{run_id}/cutover`를 호출합니다.
5. 전환 뒤에는 `POST /runs/{run_id}/finish`, 전환 전 중단은 `POST /runs/{run_id}/stop`을 호출합니다. 응답의 `state`가 `incomplete`면 같은 호출을 다시 합니다.

새로고침 뒤에는 `GET /runs`로 진행 중이거나 정리가 남은 실행을 찾습니다.

## API

| 호출 | 용도 | 응답 |
| --- | --- | --- |
| `GET /clouds` | 클라우드별 등록 여부와 필요한 키 이름 | `CloudStatus[]` |
| `PUT /clouds/{cloud}` | 클라우드 키 저장(덮어쓰기). 값은 돌려주지 않음 | `CloudStatus`, 형식 오류 422 |
| `DELETE /clouds/{cloud}` | 키 삭제. VPN이 남은 실행이 있으면 지우지 않습니다 | 204 |
| `POST /runs` | 실행 시작 | 202 `{run_id}`, 입력 오류 422 `{detail, fields}`, 키 없음 409 |
| `GET /runs` | 실행 중이거나 정리가 남은 실행 목록 | `RunSummary[]` |
| `GET /runs/{run_id}` | 상태 | `MigrationRunStatus`, 없으면 404 |
| `POST /runs/{run_id}/retry` | 저장된 입력으로 재실행. 비밀번호를 다시 받지 않음 | 202 `{run_id}`, 불가 409 |
| `POST /runs/{run_id}/cutover` | 전환 승인 `{approved_by}` | `CutoverResult`, 불가 409 |
| `POST /runs/{run_id}/finish` | 전환 뒤 정리. 오래 걸림 | `FinishResult` |
| `POST /runs/{run_id}/stop` | 전환 전 중단과 정리. 오래 걸림 | `FinishResult` |

`POST /runs` 입력의 `source.db.database`, `target.db.database`는 선택 값입니다. 서버에 DB가 여러 개면 처음부터 넣거나 `database_choices`를 보고 고릅니다.

## 상태 응답

```ts
interface MigrationRunStatus {
  run_id: string;
  job_id: string;
  route: { source: { cloud: string; region: string }; target: { cloud: string; region: string } } | null;
  state: "pending" | "running" | "waiting_approval" | "paused" | "succeeded" | "failed" | "aborted" | "stopped" | "unknown";
  phase: string;           // 아래 단계 표
  step: number | null;     // 1~12. 전환 시작부터는 null
  last_step: 12;
  progress: number;        // 0~100. 에이전트 작업 등록 전에는 0
  message: string | null;
  error: { code: string; message: string } | null;
  database_choices: { source: string[]; target: string[] } | null;
  metrics: RunMetrics;
  ready_for_cutover: boolean;
  cleanup_required: boolean;
  remaining: string[];
}
```

작업 등록 전에는 `progress`가 움직이지 않으니 진행 표시는 `step / last_step`을 쓰고, 등록 뒤의 복사 진행은 `metrics.copy_percent`를 씁니다.

`metrics`에는 값이 있는 키만 옵니다. 없는 값은 화면에 미제공으로 표시합니다.

| 키 | 형식 | 의미 |
| --- | --- | --- |
| `copy_percent`, `tables`, `rows_checked`, `mismatch`, `measured_at` | 숫자, 객체, 정수, 정수, ISO 8601 | 초기 복사와 비교 |
| `replication_lag_bytes` | 정수 | 복제 지연 |
| `measure_error` | 문자열 | 지표 측정 실패. 있으면 위 값이 오래됐을 수 있음 |
| `serving` | `source` \| `target` | 쓰기 위치. 전환을 시도한 뒤 확실하지 않으면 키가 없음 |
| `target_db_network` | `PRIVATE` \| `PUBLIC` | 목적지 DB 네트워크 |
| `cutover_outcome` | `not_started` \| `rolled_back` \| `switched` \| `post_switch_failed` \| `unknown` | 전환 결과 |
| `cutover_seconds`, `cutover_seconds_source`, `cutover_steps`, `cutover_sequence`, `verification` | 숫자, `worker` \| `agent`, 배열, `VERIFIED` \| `INCOMPLETE`, 비교 상태 | 전환 진행 |
| `cdc_error` | 문자열 | 실패 내용. `error.message`와 같은 내용 |
| `simulated` | `true` | 모의 에이전트 |

## 단계

| step | phase | 내용 |
| --- | --- | --- |
| 3~6 | `inputs` | 입력 저장, DB 조회, 실행 모델 변환, 시작 전 검사 |
| 7 | `vpn` | VPN 구축 |
| 8 | `probe` | DB 접속 확인 |
| 9 | `probe` | DB·테이블 조회 |
| 10 | `secrets`, `job` | 계정 전달, 작업 등록 |
| 11 | `source_prepare` | 원본 준비 |
| 12 | `target_prepare`, `precheck`, `replication_setup`, `snapshot`, `streaming`, `cutover_ready` | 목적지 준비, 복제, 승인 대기 |
| null | `cutover`, `stabilizing`, `closed` | 전환과 종료 |

실패하면 `step`과 `phase`는 멈춘 단계에 그대로 남습니다.

## 오류 코드

화면은 `error.code`로 안내를 나누고 `error.message`를 함께 보여 줍니다. 모르는 코드는 `message`만 표시합니다. `message`에 비밀값과 클라우드 API 원문은 들어가지 않으며, 한국어와 영어가 섞일 수 있습니다.

| 분류 | 코드 | 화면 처리 |
| --- | --- | --- |
| DB 선택 | `database_selection_required` | `database_choices`로 선택 후 retry |
| | `invalid_database_selection`, `no_database` | DB 이름과 서버 확인 |
| 테이블 | `no_primary_key`, `table_not_readable`, `partitioned_table`, `invalid_table_name`, `no_source_tables` | 메시지의 테이블을 고친 뒤 retry |
| 조회 | `lookup_failed`, `invalid_route` | 클라우드 권한과 DB 구성 확인 |
| 시작 전 검사 | `source_public_access`, `cidr_overlap`, `invalid_cidr`, `missing_cidr`, `node_outside_vcn`, `invalid_source`, `source_busy` | 원본 설정이나 네트워크 수정 후 retry |
| 클러스터 | `agent_not_ready`, `agent_unavailable`, `invalid_agent_response`, `preflight_failed`, `agent_error`, `agent_unreachable`, `catalog_unavailable`, `invalid_catalog_response` | 잠시 뒤 retry, 반복되면 운영자 확인 |
| VPN | `tunnel_timeout`, `terraform`, `credentials`, `peer_changed`, `invalid_output`, `invalid_plan`, `route_mismatch`, `unsupported_cloud` | retry 또는 stop으로 정리 |
| | `context_changed`, `legacy_context`, `context_version`, `invalid_context`, `invalid_psk`, `missing_psk`, `cleanup_failed`, `busy` | stop으로 정리한 뒤 새 실행 |
| 접속 | `db_unreachable` | 메시지의 주소와 라우팅 확인 후 retry |
| 설정 | `missing_setting` | 백엔드 환경변수 확인 |
| 복제 작업 | `job_failed` | 에이전트 오류. 메시지 표시 후 stop 판단 |
| 상태 | `status_unknown` | 에이전트와 통신 실패. 다음 조회에서 다시 확인 |
| 그 밖 | `internal`, `stopped` | 메시지 표시 후 retry |

## 화면에서 바꿀 곳

- 경로 문구는 `route`를 읽습니다. `layout.tsx`의 AWS → GCP 고정 문구를 대체합니다.
- 전환 화면은 `serving`의 `source`, `target`으로 판정합니다. `dashboard.ts`의 `servingSide`는 `aws`, `gcp`만 인정합니다.
- 네트워크 표시는 `cloud_sql_private_ip` 대신 `target_db_network`를 읽습니다.
- `copy_percent`, `replication_lag_bytes`처럼 지금 쓰는 지표 키는 이름이 같습니다.
