# assessment

discovery + planning 통합 (11/4). 담당: 김준석(DB·네트워크·원본 권한) · 이주호(앱 + 결과 조립) · 리뷰: 박준서, 안재현, 박재현

## 책임
원본(AWS·DB)을 **읽기만** 해서 이전 가능 여부를 **코드로** 판정하고, 한 번의 `run()` 결과로
provisioning 이 쓸 `plan.target_infra` 와 db_migration 이 쓸 `source` 를 함께 돌려줍니다.

- 필수 조건은 코드로 검사합니다 (`rules.py`). AI 판단으로 통과시키지 않습니다.
- 조회하지 못한 값은 지어내지 않고 `None` + `gaps` 로 남깁니다 → 해당 check 는 `unknown` → 승인 불가.
- 키·비밀번호 원문은 받지 않습니다. 이름표(`secret://...`)만 받고, 조회할 때만 꺼냅니다.
  이름은 db_migration #31 저장 API 규칙을 따릅니다: AWS 키 `secret://<run_id>/source-cloud`, DB 관리자 `secret://<run_id>/source-db-admin`.

## 진입 함수
```python
from app.features.assessment.service import run
out = run(AssessmentInput(...), ctx)   # ctx.report(state, progress, metrics, message)
```
단독 확인: `POST /assessment/run` (body = `AssessmentInput`), 조회 없이 계획만: `POST /assessment/plan` (body = `PlanningInput`)

- 진행 보고: 5 연결 → 20 DB·앱 동시 조회 → (50 앱 끝, DB 대기) → 80 판정 → (90 AI) → 100, 모두 `running`. 단계 성공은 `run()` 반환으로 workflows 가 정하고, 승인 ① 대기는 workflows 게이트가 처리 (PR #22 `RunContext`).
- 속도: DB 쪽(RDS → bastion → SSM 터널 → PG)과 앱 쪽(ECS·ECR)은 서로 기다릴 게 없어 스레드 2개로 동시에 조회합니다. AWS 클라이언트는 (서비스, 리전)마다 한 번만 만들어 재사용하고(`aws.CachedSession`), 호출마다 연결 5초·읽기 20초 제한과 표준 재시도(총 3번)를 겁니다. botocore 기본값(60초·legacy 재시도)이면 응답 없는 엔드포인트 하나에 단계가 몇 분씩 멈출 수 있습니다.
- workflows `MigrationRequest`(PR #22) 와 이름을 맞춤: `github_repo_url`, `aws_region`, `ecs_service`, `rds_instance_id`, `target_region`.
  키는 `aws_role_ref`(역할 ARN) 대신 `aws_credentials_ref`(키 이름표) — 원본 키 방식 합의(안재현) 후 맞춥니다.

실제 원본으로 확인 · fixture 만들기 (계정 ID · 네트워크 ID · RDS 주소는 자동으로 가림):
```
pip install -e ".[dev]"
python -m app.features.assessment.cli --profile <읽기 전용 프로필> --aws-region ap-northeast-2 \
    --out tests/assessment/fixtures/output.real.json
```

## 파일
| 파일 | 담당 | 하는 일 |
| --- | --- | --- |
| `schemas.py` | 공동 | 계약. 상대 쪽 모델을 바꿀 땐 상대 리뷰 |
| `service.py` | 이주호 | `run()`: 세션 → db·app 조회 → 합침 → plan → `AssessmentOutput` |
| `aws.py` | 공동 | 키 이름표로 AWS 세션 만들기 |
| `db.py` | 김준석 | `collect(inp, session) -> DbAssessment`. RDS·네트워크 → bastion 터널 → PG 카탈로그 순서로 조회. 예외를 던지지 않고 실패는 gaps 로 남김 |
| `rds.py` | 김준석 | RDS·VPC·라우팅 테이블 조회 → `DbInfo`·`SourceDb`·`SourceNetwork` (`pending-reboot` 이면 논리 복제를 켜진 것으로 치지 않음) |
| `bastion.py` | 김준석 | 태그 `Purpose=alpha-assessment-bastion` 인 bastion 을 찾고 SSM 포트포워딩 터널을 열고 닫음 (프로비저닝은 하지 않음) |
| `refs.py` | 김준석 | `db_admin_ref`(`secret://` · `env://`) → `{user, password}`. 개발용 ASM 폴백은 환경변수로만 켬 |
| `pg_connect.py` · `pg_tables.py` | 김준석 | PG 읽기 전용 접속(psycopg 가 있으면 사용, 없으면 psql) · 카탈로그 → `TableInfo`·`wal_level` |
| `app.py` | 이주호 | ECS·ECR 조회 → `AppAssessment` |
| `manual.py` | 이주호 | `source_kind="manual"`: 사람이 입력한 원본 값 → `DbAssessment`·`AppAssessment` (AWS 조회와 같은 형식). 확인 안 한 값이라 gaps 에 '직접 입력' 을 남기고, 비밀처럼 보이는 환경 변수는 버림 |
| `cloud_source.py` | 이주호 | `source_kind="gcp"`·`"azure"`: 클라우드 API(REST, 읽기 전용 GET)로 원본 앱·DB 를 조회해 같은 형식을 만듦. GCP = Cloud Run v2 + Cloud SQL Admin v1, Azure = Container Apps + PostgreSQL Flexible Server. 비밀 값은 읽지 않고 참조 이름만, 모르는 값은 None. 테이블은 DB 에 직접 붙을 수 있을 때만 읽고, 못 붙으면 gaps |
| `plan.py` · `rules.py` | 이주호 | 필수 조건 판정, 목적지 매핑, risks·summary |
| `ai.py` | 이주호 | AI 보강 (기본 꺼짐, `ALPHA_AI_ENABLED=1` + `pip install -e ".[ai]"`). summary 3줄·설명용 risks 만. 판정·target_infra 는 안 바꿈, 실패하면 규칙 결과 |
| `ai_mapping.py` | 이주호 | AI 제안 (같은 스위치). 규칙에 없는 환경 변수 이름의 목적지 대응·빠진 의존성을 `plan.suggestions` 에만 덧붙임. 판정·target_infra 는 안 바꿈. 비밀은 보내지 않고, 제안은 입력에 없는 이름·지어낸 숫자·주소를 항목별로 버림. 실패하면 빈 목록. 평가: `scripts/eval_assessment_ai_mapping.py` |
| `cli.py` | 이주호 | 실제 원본 조회 → JSON (기본으로 가림) |

## 입력 — `AssessmentInput` (workflows → assessment)
| 필드 | 설명 |
| --- | --- |
| `run_id`, `github_repo_url` | workflows · 사용자 |
| `source_kind` | 원본 읽는 방법. `aws`(기본, AWS 계정 조회) / `manual`(직접 입력) / `gcp`·`azure`(클라우드 API 조회). 어느 쪽이든 출력 형식은 같음 |
| `cloud_source` | `source_kind="gcp"`·`"azure"` 면 필수. `{region, app_name, db_instance, credentials_ref, project_id(GCP), subscription_id·resource_group(Azure)}`. GCP `credentials_ref` 는 서비스 계정 키 JSON 이름표(비우면 서버 ADC), Azure 는 `{tenant_id, client_id, client_secret}` 이름표(필수). 필요한 권한: GCP `roles/run.viewer`·`roles/cloudsql.viewer`, Azure `Reader` |
| `manual` | `source_kind="manual"` 이면 필수. `{cloud, region, app: {service_name, image_uri, container_port, vcpu, memory_mib, health_check_path, env_vars, db_user}, db: {engine_version, host, port, database_name, memory_gib, vcpu, storage_gb, publicly_accessible, logical_replication, tables}}`. 모르는 값은 비우면 '확인 안 됨'·기본값 |
| `aws_credentials_ref` | `source_kind="aws"` 면 필수. 원본 AWS 키 이름표. `secret://...`(backend DB, `core/secrets.resolve`) 또는 로컬 개발용 `profile:<이름>`(`ALPHA_ALLOW_AWS_PROFILE=1` 일 때만, cli.py 가 켬). 저장 값은 `{aws_access_key_id, aws_secret_access_key, aws_session_token?}` (#31 `source-cloud` 와 같음) |
| `db_admin_ref` | 원본 DB 관리자 계정 이름표 (db.py 가 PG 카탈로그 조회에 사용) |
| `aws_region` / `target_region` | 원본 AWS 리전(`source_kind="aws"` 면 필수) / 목적지 리전 |
| `ecs_cluster`, `ecs_service`, `rds_instance_id` | 선택. 후보가 여러 개일 때만. 비우면 하나뿐일 때 자동 선택, 여러 개면 고르지 않고 gaps |

## 출력 — `AssessmentOutput`
| 필드 | 받는 쪽 | 내용 |
| --- | --- | --- |
| `source` | db_migration (안재현) | cdc 명세 2절 칸: `account_id`, `region`, `db.*`, `network.*`, `app.db_user`, `tables`("schema.table"), `credentials_ref.cloud/db_admin` |
| `plan` | 승인 ① 화면 (박재현), `plan.target_infra` → provisioning (박준서) | `MigrationPlan`: `target_infra`, `checks`, `risks`, `summary`, `steps`, `approvable`, `blocking` |
| `gaps` | 승인 ① 화면 | 조회 실패 항목 |

## db.py 가 읽는 것 (읽기 전용 권한)
`rds:DescribeDBInstances`, `rds:DescribeDBParameters`, `ec2:DescribeVpcs`, `ec2:DescribeSubnets`, `ec2:DescribeRouteTables`,
`ec2:DescribeInstances`, `ssm:DescribeInstanceInformation`, 터널을 여는 `ssm:StartSession`(대상 bastion 과 문서 `AWS-StartPortForwardingSessionToRemoteHost`)·`ssm:TerminateSession`

- **DB 접속은 bastion 경유**: 원본 RDS 는 퍼블릭 액세스가 꺼져 있고 VPN 은 `run()` 에서 나중에 만들어지므로, 이미 떠 있는 bastion 을 **태그로 찾아** SSM 터널로 PG 에 접속합니다. 없거나 SSM 이 Online 이 아니면 PG 단계를 건너뛰고 gaps 에 남깁니다 (프로비저닝은 이 모듈이 하지 않음).
- **PG 에는 읽기만**: `default_transaction_read_only=on` 으로 접속하고 SELECT/SHOW 만 실행합니다. 비밀번호는 argv 가 아니라 `PGPASSWORD` 환경변수로만 자식 프로세스에 전달합니다.
- **실행 서버 조건**: `aws` CLI 와 `session-manager-plugin` 이 필요합니다. PG 접속은 `psycopg` 가 있으면 그것을, 없으면 `psql` 을 씁니다 (psycopg 는 팀 합의 전이라 의존성에 넣지 않음).
- **환경변수 (모두 선택)**
  | 이름 | 뜻 |
  | --- | --- |
  | `ALPHA_SOURCE_DB_NAME` | 조회할 앱 DB 이름. 우선순위: 이 변수 > ECS 태스크 정의의 `PGDATABASE` 힌트(`PGHOST` 가 원본 DB 호스트인 컨테이너의 평문 값, 하나로 정해질 때만) > RDS `DBName` > `postgres`. 힌트로 얻은 이름의 DB 가 없으면 `postgres` 로 대신하지 않고 비운 뒤 gaps 에 남김. 이름을 정하지 못해 폴백했는데 `postgres` 외 DB 가 있으면 앱 DB 를 확정하지 않고 gaps 에 남김 |
  | `ALPHA_ALLOW_ENV_REFS=1` | **개발 전용.** `db_admin_ref` 의 `env://PREFIX`(PREFIX_USER/PREFIX_PASSWORD) 를 허용. 꺼져 있으면 거부(화면 입력으로 서버 환경변수를 읽지 못하게). 운영에선 `secret://` 사용 |
  | `ALPHA_DB_ADMIN_ASM_FALLBACK=1` | **개발 전용.** `db_admin_ref` 가 없을 때 Secrets Manager 의 RDS 시크릿에서 계정을 읽음. 이름표가 있으면 실패해도 폴백하지 않음 |
- 앱 DB 이름 힌트용으로 `ecs:ListClusters`, `ecs:ListServices`, `ecs:DescribeServices`, `ecs:DescribeTaskDefinition` 읽기 권한도 씁니다 (`app_hint.py`, 보조. 실패하면 조용히 건너뜀).
- `db_admin_ref` 의 값 형식은 db_migration(PR #31) 과 같은 `{"user": ..., "password": ...}` 입니다.

## app.py 가 읽는 것 (읽기 전용 권한)
`sts:GetCallerIdentity`, `ecs:ListClusters`, `ecs:ListServices`, `ecs:DescribeServices`, `ecs:DescribeTaskDefinition`,
`ecr:DescribeImages`, `elasticloadbalancing:DescribeTargetGroups`

- 이미지·포트·CPU·메모리·환경 변수·비밀 참조(ARN)는 태스크 정의, digest 는 ECR, 상태 확인 경로는 컨테이너 healthCheck → 없으면 로드밸런서 대상 그룹.
- **Cloud Run 호환 검사**(`cloud_run_compatible`): 태스크 정의에 Cloud Run 으로 옮길 수 없는 설정이 있으면 fail 입니다 — `privileged`, 호스트 장치·capability 추가, UDP 포트, GPU, ARM64, `pidMode`·`ipcMode` 공유, 호스트 경로·Docker 볼륨 마운트, Cloud Run 최대 크기(8 vCPU · 32768 MiB) 초과. 시작 명령 재정의, 함께 도는 다른 컨테이너, EFS·임시 볼륨, App Mesh 는 승인을 막지 않고 risks 에 남깁니다. 값(명령어·경로)은 읽지 않고 설정 이름만 남기며, 직접 입력 원본은 태스크 정의가 없어 이 검사가 빠집니다.
- 앱 DB 사용자는 태스크 정의의 `PGUSER` → `source.app.db_user`, `target_infra.db_user`.
- 평문 환경 변수 중 이름이 비밀처럼 보이는 것(`PASSWORD`, `SECRET`, `TOKEN`, `API_KEY`, `…_KEY` …)은 **값을 버리고** gaps 에 남깁니다. 목적지에서 앱이 값을 못 받으니 승인자가 알아야 하기 때문입니다. 단, provisioning 이 목적지에서 직접 채우는 이름(`PGPASSWORD` 등 `PROVISIONING_INJECTED_ENV`)은 값만 버리고 gaps 에는 남기지 않습니다 (승인자가 할 일이 없는 경고라서 "원본 수집 누락"으로 보였던 문제, #79).
- 이름이 평범해도 값에 비밀이 들어 있으면(`postgres://user:비밀번호@…` 같은 접속 주소, AWS 액세스 키, 개인키, JWT, 알려진 API 토큰 형식) 똑같이 값을 버립니다. gaps 에는 이름만 남깁니다.
- AI 보강(`ai.py`)에 보내는 계획에서는 AWS 계정 ID 를 `<aws-account>` 로 가립니다.
- AWS 오류는 메시지 대신 오류 코드만 남깁니다.

provisioning 도 같은 키 이름표(`source.credentials_ref.cloud`)를 `resolve()` 로 꺼내 원본 ECR 이미지를 복사합니다 (박준서, 11/5).
그래서 사용자가 만드는 키 정책에는 위 읽기 권한에 더해 이미지 내려받기 권한도 넣어야 합니다:
`ecr:GetAuthorizationToken`(리소스 `*`), `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`, `ecr:BatchCheckLayerAvailability`

## 매핑 규칙 (원본 → 목적지)
| 원본 | 목적지 | 규칙 |
| --- | --- | --- |
| 이미지 저장소 이름 (없으면 ECS 서비스 이름) | `service_name` | GCP 이름 규칙(소문자·숫자·하이픈)으로 바꿈. ECS 서비스 이름엔 자동 생성 접미사가 붙을 수 있어 저장소 이름을 먼저 씀 |
| 위와 같은 이름 (규칙 적용 전) | `source_service_name` | 클라우드 중립. Azure 등 다른 목적지가 자기 이름 규칙을 적용. GCP Terraform 에는 안 넘어감 |
| Fargate CPU 단위 | `cpu` (vCPU, 숫자) | `ceil(units/1024)`, 최소 1 |
| Fargate 메모리 MiB | `memory` (MiB, 숫자) | 최소 512 |
| RDS PostgreSQL 10~16 | `db_version` = `"16"` | 16 초과(다운그레이드)·10 미만은 fail |
| RDS 인스턴스 클래스 | `db_tier` | 원본 메모리 이상인 가장 작은 등급. 지금은 micro(1GiB) → `db-g1-small` 만. 그 외는 넘기지 않음(기본값) |
| RDS 인스턴스 클래스 | `db_memory_gib` · `db_vcpu` | 클라우드 중립 DB 크기 (원본 메모리·vCPU). 목적지마다 자기 등급으로 바꿈 (Azure·AWS는 provisioning 의 설정 카드가 원본 메모리 이상인 가장 작은 등급으로 변환). `rules.py` 의 `_RDS_CLASS_SIZES` 표(t3·t4g·m5·m6g·m6i·m7g·m7i·r5·r6g·r6i 등 142개 클래스, EC2 API 로 확인)에 있는 클래스만, 그 외는 넘기지 않음. GCP Terraform 에는 안 넘어감 |
| RDS 디스크 GB | `db_disk_size_gb` | 원본과 같게, 최소 10 |
| 환경 변수 | `env_vars` | PG*·APP_ENV·PORT 는 provisioning 이 채우므로 제외 |
| 비밀 참조 | `secret_refs` | 이름 → `{service_name}-{변수명 소문자}`. 값 이전은 별도 작업 |
| 이미지 | `source_image` | 원본 ECR 주소. digest 를 알면 `저장소@sha256:...` 로 고정 (태그가 바뀌어도 같은 이미지). Artifact Registry 복사는 provisioning |
| RDS 퍼블릭 액세스 켜짐 | `risks` (high) | CDC 가 VPN 을 못 탐. 이전 전에 꺼야 함 (cdc 명세 2절) |
| 값 없음 | 기본값 (포트 8080, `/health/ready`, 1 vCPU, 512MiB) | 기본값을 쓰면 `risks` 에 남김 |

## 미정 사항 (합의 필요)
- [x] `target_infra` 필드 이름 ↔ Terraform 변수 1:1 (박준서, 11/3 합의, PR #26)
- [x] db.py · app.py 반환은 Pydantic 모델 (`DbAssessment` / `AppAssessment`) (김준석, 11/4)
- [ ] 원본 AWS 키: 사용자가 미리 만든 IAM 사용자 키를 등록 (김준석 제안, 안재현 확인 대기)
- [ ] `psycopg` 의존성: 합의 전이라 psql 폴백으로 동작. 합의되면 폴백 제거 (김준석)
- [ ] bastion 프로비저닝: 지금은 이미 떠 있는 bastion 을 태그로 찾아 쓰기만 함. 없을 때 만들지 여부와 방식(terraform / boto3)은 미정 (김준석)
- [ ] `core/secrets.py` 의 `put_secret` / `resolve` — PR #29(안재현) 의 dict 반환에 맞춤. 머지 전엔 `secret://` 이름표는 gaps 로 끝남
- [ ] PG 버전 기준: cdc 명세 "16 이상" vs 판정 "10~16" (안재현)
- [ ] `db_tier` 매핑 규칙 확인 (박준서)
- [ ] AI 모델·호출 방식 (risks·summary 보강)
- [ ] `Reporter` → workflows `RunContext` 로 교체 (박재현, PR #22). 모양은 같고, 끝 보고도 `running` 으로 맞춤

## fixture
`apps/backend/tests/assessment/fixtures/`
- `inventory.sample.json`: **실제 조회 결과 아님.** `samples/target-app` README 와 박준서님이 알려준 원본 RDS 사양(db.t3.micro, 20GB, 사용자 alpha)으로 손으로 만든 입력입니다. 실제 조회 결과가 나오면 교체합니다.
- `plan.sample.json`: 위 입력으로 `plan.build()` 를 실제 실행한 결과.
- `tests/assessment/fakes.py`: AWS 를 부르지 않는 가짜 세션 (계정 ID 는 예시 값).
