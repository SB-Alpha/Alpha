# planning

담당: 이주호 · 리뷰: 박준서, 안재현

## 책임
`SourceInventory`(discovery) 와 목적지 선택(GCP 리전)으로 **승인할 수 있는 `MigrationPlan`** 을 만듭니다.

- 필수 조건은 **코드로** 검사합니다 (`rules.py`). AI 판단으로 통과시키지 않습니다.
- AI 는 위험 설명·요약 초안만 씁니다. **AI 호출이 실패해도 규칙 기반 계획은 나옵니다.**
- 값이 없으면 `unknown` 으로 두고 승인 불가(`approvable=false`)로 표시합니다. 가짜 통과 없음.

## 진입 함수
```python
from app.features.planning.service import run
plan = run(PlanningInput(...), ctx)   # ctx.report(state, progress, metrics, message)
```
단독 확인: `POST /planning/plan` (body = `PlanningInput`)

## 입력 — `PlanningInput`
| 필드 | 출처 | 설명 |
| --- | --- | --- |
| `inventory.app` | discovery | 서비스 이름, 이미지 URI·digest, 포트, Fargate CPU·메모리, 상태 확인 경로, 비밀 아닌 환경 변수(이름→값), 비밀 **참조**(이름→AWS ARN) |
| `inventory.db` | discovery | 엔진·버전, DB 이름, 사용자 이름, 인스턴스 클래스, 디스크 GB, 테이블(행 수·PK 여부), 논리 복제 설정(`rds.logical_replication`, `wal_level`) |
| `inventory.gaps` | discovery | 수집 실패 항목 |
| `target.region` | 사용자 | GCP 리전 |

> 임시: `InventoryForPlanning` 은 planning 이 읽는 필드만 적은 것입니다. 김준석님 `SourceInventory` 확정 후 그 모델로 교체합니다.

## 출력 — `MigrationPlan` (→ 승인 ① 화면, provisioning, db_migration)
| 필드 | 설명 |
| --- | --- |
| `target_infra` | provisioning(Terraform) 입력. 필드 이름 = Terraform 변수 이름 (박준서님과 11/3 합의). `to_tfvars()` 로 평평한 tfvars JSON 이 되며, 값이 없는 키는 빠져서 provisioning 기본값을 씁니다 |
| `checks` | 필수 조건 4개: PG 버전 호환 · 논리 복제 · PK 존재 · 이미지 확보. 결과 `pass`/`fail`/`unknown` |
| `risks` | 위험 목록 (`source`: `rule` / `ai`) |
| `summary` | 승인 화면용 3줄 (`summary_source`: `rule` / `ai`) |
| `steps` | 실행 순서 (승인 ①·② 지점 포함) |
| `approvable` / `blocking` | 모든 필수 조건이 `pass` 일 때만 `true`. 아니면 이유 목록 |

## 매핑 규칙 (원본 → 목적지)
| 원본 | 목적지 | 규칙 |
| --- | --- | --- |
| Fargate CPU 단위 | `cpu` (vCPU, 숫자) | `ceil(units/1024)`, 최소 1 |
| Fargate 메모리 MiB | `memory` (MiB, 숫자) | 최소 512 |
| RDS PostgreSQL 10~16 | `db_version` = `"16"` | 16 초과(다운그레이드)·10 미만은 fail |
| RDS 인스턴스 클래스 | `db_tier` | 원본 메모리 이상인 가장 작은 등급. 지금은 micro(1GiB) → `db-g1-small` 만. 그 외는 넘기지 않음(기본값) |
| RDS 디스크 GB | `db_disk_size_gb` | 원본과 같게, 최소 10 |
| 환경 변수 | `env_vars` | PG*·APP_ENV·PORT 는 provisioning 이 채우므로 제외 |
| 비밀 참조 | `secret_refs` | 이름 → `{service_name}-{변수명 소문자}`. 값 이전은 별도 작업 |
| 이미지 | `source_image` | 원본 ECR 주소 그대로. Artifact Registry 복사는 provisioning |
| 값 없음 | 기본값 (포트 8080, `/health/ready`, 1 vCPU, 512MiB) | 기본값을 쓰면 `risks` 에 남김 |

## 미정 사항 (합의 필요)
- [x] `target_infra` 필드 이름 ↔ Terraform 변수 1:1 (박준서, 11/3 DM 합의)
- [ ] `db_tier` 매핑 규칙 확인 (박준서)
- [ ] `SourceInventory` 필드 확정 후 입력 모델 교체 (김준석)
- [ ] 승인 화면에 보일 요약 형태 (박재현)
- [ ] AI 모델·호출 방식 (risks·summary 보강)
- [ ] `Reporter` → workflows `RunContext` 로 교체 (박재현)

## fixture
`apps/backend/tests/planning/fixtures/`
- `inventory.sample.json`: **실제 discovery 결과 아님.** `samples/target-app` README 와 박준서님이 알려준 원본 RDS 사양(db.t3.micro, 20GB, 사용자 alpha)으로 손으로 만든 입력입니다. 실제 inventory fixture 가 나오면 교체합니다.
- `plan.sample.json`: 위 입력으로 `run()` 을 실제 실행한 결과.
