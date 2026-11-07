# target-app — 이전 대상 샘플 서비스

플랫폼이 **AWS(ECS Fargate + RDS PostgreSQL) → GCP**로 옮길 대상 서비스입니다.
업무 항목(work item) 하나를 로그인 없이 조회·등록·수정·삭제합니다. 앱 자체는 평가 대상이 아니며, 이전 실험용으로 최소 기능만 둡니다.

> 이 폴더는 **이전 대상 앱**입니다. 플랫폼 코드(`apps/`)와 섞지 않습니다.

## 1. 한눈에 보기

| 항목 | 값 |
| --- | --- |
| 구성 | 웹(정적 페이지) → API → PostgreSQL, **컨테이너 1개** + DB 1개 |
| 스택 | Python 3.12, FastAPI 0.115.6, Uvicorn 0.32.1, psycopg 3.2.3 (+ psycopg-pool) |
| DB | **PostgreSQL 16** |
| 컨테이너 포트 | **8080** (`PORT`로 변경 가능) |
| 헬스체크 | live `GET /health/live` · ready `GET /health/ready` (DB 확인) |
| 기본키 | **UUID** (`gen_random_uuid()`) — 시퀀스 상태 이전 불필요 |
| 스키마 적용 | 앱 시작 시 `db/schema.sql`을 자동 적용 (`CREATE TABLE IF NOT EXISTS`, 반복 실행 안전) |
| 이미지 사용자 | 비루트 `appuser` (uid 10001) |
| 제외 | 로그인·세션·캐시·메시지 큐·예약 작업·파일 업로드 |

## 2. 환경변수 (이름만 — 값은 커밋 금지, 레포 PUBLIC)

DB 연결은 PostgreSQL 표준(libpq) 변수를 그대로 씁니다. ECS에서는 `PGPASSWORD`를 Secrets Manager 등 비밀 참조로 주입하세요.

| 이름 | 필수 | 설명 |
| --- | :---: | --- |
| `PGHOST` | ✅ | DB 호스트 (RDS 엔드포인트 / Cloud SQL 주소) |
| `PGPORT` |  | 기본 5432 |
| `PGDATABASE` | ✅ | DB 이름 |
| `PGUSER` | ✅ | 앱 계정 |
| `PGPASSWORD` | ✅ | 앱 계정 비밀번호 (**비밀 참조로 주입**) |
| `PGSSLMODE` |  | RDS·Cloud SQL은 `require` 권장 |
| `PORT` |  | 기본 8080 |
| `APP_ENV` |  | 처리 환경 표시 (예: `aws`, `gcp`). 응답 헤더 `X-Served-By`와 로그에 그대로 찍힘 |
| `LOG_LEVEL` |  | 기본 `INFO` |
| `WORKER_SECRET` |  | 설정하면 `/health/*` 밖의 요청은 `X-Alpha-Worker-Secret` 헤더가 같은 값일 때만 받고, 나머지는 403. Cloudflare Worker와 같은 값을 **비밀 참조로 주입** (#34). **목적지에만 설정** — Worker는 AWS 원본에 헤더를 보내지 않으므로 원본에 넣으면 업무 요청이 전부 403 |
| `REQUIRE_WORKER_SECRET` |  | `true`면 `WORKER_SECRET`이 없을 때 모든 요청을 403으로 거절. Worker가 헤더를 보내는 목적지에서만 켜고, AWS 원본·로컬에서는 끔 |

## 3. 실행

### 로컬 (Docker Compose — PostgreSQL 16 포함)

```bash
cd samples/target-app
docker compose up -d --build
open http://localhost:8080          # 웹 화면
curl http://localhost:8080/health/ready
docker compose exec app python -m app.seed --count 1000   # 초기 데이터 1,000건
docker compose down -v               # 정리 (DB 볼륨 포함)
```

Compose의 DB는 `wal_level=logical`로 떠서 RDS의 논리 복제 설정과 같은 조건으로 CDC를 미리 시험할 수 있습니다. Compose 안의 비밀번호는 로컬 전용 값입니다.

### 이미지 빌드 (CI·배포용)

```bash
cd samples/target-app
docker buildx build --platform linux/amd64 -t <registry>/target-app:<git-sha> .
docker run --rm -p 8080:8080 \
  -e PGHOST=<host> -e PGDATABASE=<db> -e PGUSER=<user> -e PGPASSWORD=<secret> -e PGSSLMODE=require \
  -e APP_ENV=aws <registry>/target-app:<git-sha>
```

- 빌드 컨텍스트는 `samples/target-app/`입니다.
- Fargate 기본 아키텍처는 **x86_64(amd64)** 입니다. Apple Silicon에서 빌드하면 `--platform linux/amd64`를 꼭 붙이세요.

### 테스트

실제 PostgreSQL에 붙어서 돕니다 (`docker compose up -d db` 후).

```bash
cd samples/target-app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
PGHOST=127.0.0.1 PGDATABASE=workitems PGUSER=app PGPASSWORD=local-only pytest
```

## 4. DB 스키마 (실제 DDL — `db/schema.sql`)

```sql
CREATE TABLE IF NOT EXISTS work_items (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    title       TEXT        NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    description TEXT        NOT NULL DEFAULT '',
    status      TEXT        NOT NULL DEFAULT 'todo'
                            CHECK (status IN ('todo', 'in_progress', 'done')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS work_items_created_at_idx ON work_items (created_at DESC);
```

- 업무 테이블은 `work_items` **1개**입니다. 기본키가 있으므로 Debezium의 수정·삭제 이벤트에 기본 `REPLICA IDENTITY`로 충분합니다.
- `updated_at`은 수정 API가 `now()`로 갱신합니다 (트리거 없음).
- 앱 계정에 `CREATE` 권한이 있으면 시작 시 스키마가 자동 생성됩니다. 권한을 분리하려면 관리자 계정으로 위 DDL을 먼저 실행하세요. 앱 계정에 `CREATE` 권한이 없으면 앱은 경고 로그만 남기고 기존 테이블로 정상 기동합니다 (CDC로 테이블을 받은 목적지 DB도 같은 경우).

## 5. API

공통: JSON, 인증 없음. 잘못된 입력·UUID 형식 오류는 `422`, 없는 항목은 `404`.
응답에 `X-Request-Id`(요청에 있으면 그대로, 없으면 생성)와 `X-Served-By`(`APP_ENV`) 헤더가 붙습니다. 처리되지 않은 서버 오류(500)에는 헤더가 빠질 수 있으나, 요청 로그에는 같은 `request_id`로 남습니다.
OpenAPI 문서: `GET /docs`.

| 메서드 | 경로 | 설명 | 성공 |
| --- | --- | --- | --- |
| GET | `/health/live` | 프로세스 생존 (DB 미확인) | 200 `{"status":"ok"}` |
| GET | `/health/ready` | DB `SELECT 1` 확인 | 200 `{"status":"ok","db":"ok"}` / 실패 503 |
| GET | `/api/work-items?limit=50&offset=0` | 목록 (최신순, `limit` 1~200) | 200 |
| GET | `/api/work-items/{id}` | 단건 조회 | 200 / 404 |
| POST | `/api/work-items` | 등록 | 201 |
| PATCH | `/api/work-items/{id}` | 부분 수정 (보낸 필드만) | 200 / 404 |
| DELETE | `/api/work-items/{id}` | 삭제 | 204 / 404 |
| GET | `/` | 웹 화면 | 200 |

필드 제약: `title` 1~200자(필수), `description` 0~2000자, `status` ∈ `todo` · `in_progress` · `done`.

### 요청·응답 예시

```bash
# 등록
curl -s -X POST http://localhost:8080/api/work-items \
  -H 'Content-Type: application/json' \
  -d '{"title":"first item","description":"smoke"}'
```
```json
{"id":"6000fae5-3f40-48bd-8396-16cacd085962","title":"first item","description":"smoke","status":"todo","created_at":"2026-11-02T06:53:19.065701Z","updated_at":"2026-11-02T06:53:19.065701Z"}
```

```bash
# 수정 (보낸 필드만 바뀜)
curl -s -X PATCH http://localhost:8080/api/work-items/6000fae5-3f40-48bd-8396-16cacd085962 \
  -H 'Content-Type: application/json' -d '{"status":"in_progress","title":"edited"}'
```
```json
{"id":"6000fae5-3f40-48bd-8396-16cacd085962","title":"edited","description":"smoke","status":"in_progress","created_at":"2026-11-02T06:53:19.065701Z","updated_at":"2026-11-02T06:53:19.100307Z"}
```

```bash
# 조회 / 목록 / 삭제
curl -s http://localhost:8080/api/work-items/6000fae5-3f40-48bd-8396-16cacd085962
curl -s 'http://localhost:8080/api/work-items?limit=20&offset=0'
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE http://localhost:8080/api/work-items/6000fae5-3f40-48bd-8396-16cacd085962   # 204
```

## 6. 요청 기록 (부하·검증 대조용)

요청마다 표준 출력에 JSON 한 줄을 남깁니다. ECS에서는 CloudWatch Logs, GCP에서는 Cloud Logging으로 그대로 모입니다.

```json
{"ts":"2026-11-02T06:53:19.131866+00:00","request_id":"4a782e09-...","method":"PATCH","path":"/api/work-items/6000fae5-...","status":200,"latency_ms":3.21,"served_by":"aws"}
```

부하 도구가 `X-Request-Id`를 직접 넣어 보내면 클라이언트 기록과 서버 로그를 요청 단위로 맞출 수 있습니다.

## 7. 배포 요구사항 (AWS — 배포 담당 확인용)

| 항목 | 요구사항 |
| --- | --- |
| ECS | Fargate, 태스크 1개, 컨테이너 포트 **8080**, `linux/amd64` 이미지 |
| 헬스체크 | ALB·대상 그룹 경로 **`/health/live`** (프로세스만 확인). `/health/ready`(DB 확인)를 쓰면 DB가 잠깐 막힐 때(예: RDS 재부팅) 정상 태스크까지 계속 교체되므로 대상 그룹에는 쓰지 않습니다. `/health/ready`는 트래픽 분배 판단(예: 전환 프록시·Cloudflare 모니터)에만 씁니다. |
| RDS | **PostgreSQL 16**, Single-AZ 1개 |
| **CDC 필수** | 사용자 정의 파라미터 그룹에 **`rds.logical_replication = 1`** 설정. 정적 파라미터라 **재시작이 필요**하므로 **DB 생성 시점에** 붙여 두세요 (나중에 켜면 운영 중 재시작 발생). |
| 연결 | `PGSSLMODE=require` 권장, `PGPASSWORD`는 비밀 참조로 주입 |
| 초기 데이터 | 배포 후 `python -m app.seed --count 1000` 1회 실행 (ECS Exec 또는 일회성 태스크) |

CDC 전용 계정·복제 슬롯·publication 구성은 `db_migration` 담당이 정합니다. 이 앱은 그 설정에 의존하지 않습니다.
