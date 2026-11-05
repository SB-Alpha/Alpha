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

- 진행 보고: 5 연결 → 20 DB → 50 앱 → 80 판정 → (90 AI) → 100, 모두 `running`. 단계 성공은 `run()` 반환으로 workflows 가 정하고, 승인 ① 대기는 workflows 게이트가 처리 (PR #22 `RunContext`).
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
| `db.py` | 김준석 | RDS·PG·네트워크 조회 → `DbAssessment` (**아직 미구현: "미구현" 을 gaps 로 반환**) |
| `app.py` | 이주호 | ECS·ECR 조회 → `AppAssessment` |
| `plan.py` · `rules.py` | 이주호 | 필수 조건 판정, 목적지 매핑, risks·summary |
| `ai.py` | 이주호 | AI 보강 (기본 꺼짐, `ALPHA_AI_ENABLED=1` + `pip install -e ".[ai]"`). summary 3줄·설명용 risks 만. 판정·target_infra 는 안 바꿈, 실패하면 규칙 결과 |
| `cli.py` | 이주호 | 실제 원본 조회 → JSON (기본으로 가림) |

## 입력 — `AssessmentInput` (workflows → assessment)
| 필드 | 설명 |
| --- | --- |
| `run_id`, `github_repo_url` | workflows · 사용자 |
| `aws_credentials_ref` | 원본 AWS 키 이름표. `secret://...`(backend DB, `core/secrets.resolve`) 또는 로컬 개발용 `profile:<이름>`(`ALPHA_ALLOW_AWS_PROFILE=1` 일 때만, cli.py 가 켬). 저장 값은 `{aws_access_key_id, aws_secret_access_key, aws_session_token?}` (#31 `source-cloud` 와 같음) |
| `db_admin_ref` | 원본 DB 관리자 계정 이름표 (db.py 가 PG 카탈로그 조회에 사용) |
| `aws_region` / `target_region` | 원본 AWS 리전 / 목적지 GCP 리전 |
| `ecs_cluster`, `ecs_service`, `rds_instance_id` | 선택. 후보가 여러 개일 때만. 비우면 하나뿐일 때 자동 선택, 여러 개면 고르지 않고 gaps |

## 출력 — `AssessmentOutput`
| 필드 | 받는 쪽 | 내용 |
| --- | --- | --- |
| `source` | db_migration (안재현) | cdc 명세 2절 칸: `account_id`, `region`, `db.*`, `network.*`, `app.db_user`, `tables`("schema.table"), `credentials_ref.cloud/db_admin` |
| `plan` | 승인 ① 화면 (박재현), `plan.target_infra` → provisioning (박준서) | `MigrationPlan`: `target_infra`, `checks`, `risks`, `summary`, `steps`, `approvable`, `blocking` |
| `gaps` | 승인 ① 화면 | 조회 실패 항목 |

## app.py 가 읽는 것 (읽기 전용 권한)
`sts:GetCallerIdentity`, `ecs:ListClusters`, `ecs:ListServices`, `ecs:DescribeServices`, `ecs:DescribeTaskDefinition`,
`ecr:DescribeImages`, `elasticloadbalancing:DescribeTargetGroups`

- 이미지·포트·CPU·메모리·환경 변수·비밀 참조(ARN)는 태스크 정의, digest 는 ECR, 상태 확인 경로는 컨테이너 healthCheck → 없으면 로드밸런서 대상 그룹.
- 앱 DB 사용자는 태스크 정의의 `PGUSER` → `source.app.db_user`, `target_infra.db_user`.
- 평문 환경 변수 중 이름이 비밀처럼 보이는 것(`PASSWORD`, `SECRET`, `TOKEN`, `API_KEY` …)은 **값을 버리고** gaps 에 남깁니다.
- AWS 오류는 메시지 대신 오류 코드만 남깁니다.

provisioning 도 같은 키 이름표(`source.credentials_ref.cloud`)를 `resolve()` 로 꺼내 원본 ECR 이미지를 복사합니다 (박준서, 11/5).
그래서 사용자가 만드는 키 정책에는 위 읽기 권한에 더해 이미지 내려받기 권한도 넣어야 합니다:
`ecr:GetAuthorizationToken`(리소스 `*`), `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`, `ecr:BatchCheckLayerAvailability`

## 매핑 규칙 (원본 → 목적지)
| 원본 | 목적지 | 규칙 |
| --- | --- | --- |
| 이미지 저장소 이름 (없으면 ECS 서비스 이름) | `service_name` | GCP 이름 규칙(소문자·숫자·하이픈)으로 바꿈. ECS 서비스 이름엔 자동 생성 접미사가 붙을 수 있어 저장소 이름을 먼저 씀 |
| Fargate CPU 단위 | `cpu` (vCPU, 숫자) | `ceil(units/1024)`, 최소 1 |
| Fargate 메모리 MiB | `memory` (MiB, 숫자) | 최소 512 |
| RDS PostgreSQL 10~16 | `db_version` = `"16"` | 16 초과(다운그레이드)·10 미만은 fail |
| RDS 인스턴스 클래스 | `db_tier` | 원본 메모리 이상인 가장 작은 등급. 지금은 micro(1GiB) → `db-g1-small` 만. 그 외는 넘기지 않음(기본값) |
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
