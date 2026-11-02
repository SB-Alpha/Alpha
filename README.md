# Alpha

Softbank Final 알파팀의 프로젝트 모노레포입니다!

## 디렉터리 구조

```
.
├─ README.md
├─ .gitignore
├─ apps/
│  ├─ frontend/                # 프론트엔드 
│  └─ backend/                 # FastAPI 플랫폼 백엔드
│     ├─ pyproject.toml
│     ├─ .env.example
│     ├─ app/
│     │  ├─ main.py            # 앱 진입점, GET /health
│     │  ├─ core/              # 공통 설정·로그
│     │  └─ features/
│     │     ├─ discovery/      # 대상 서비스 정보 수집·가공
│     │     ├─ planning/       # 배포 목적지 배포 계획 수립
│     │     ├─ provisioning/   # 목적지 인프라 구축
│     │     ├─ db_migration/   # CDC 기반 DB 이전 실행·관리
│     │     └─ workflows/      # 모듈 연결·전체 이전 흐름 실행
│     └─ tests/
├─ infra/
│  ├─ terraform/gcp/           # GCP 목적지 앱·DB 인프라 IaC
│  └─ db_migration/
│     ├─ terraform/            # CDC 실행 서버·네트워크·저장소 IaC
│     ├─ deployment/           # Kafka·Kafka Connect 실행·배포 설정
│     └─ connectors/           # Debezium Source·JDBC Sink 설정 템플릿
├─ samples/
│  └─ target-app/              # 이전 대상 샘플 서비스 (플랫폼 코드 아님)
└─ docs/ 
```

## 모듈별 책임

| 모듈           | 역할 |
|----------------|------|
| discovery      | 대상 서비스 정보를 수집·가공 |
| Planning       | 수집 정보를 기반으로 배포 목적지에 어떻게 배포할지 계획 수립 |
| provisioning   | 앞에서 주어진 정보를 기반으로 실제 구축 실행 |
| db_migration   | provisioning,discovery에서 받은 정보를 기반으로 cdc수행 |
| workflows      | 위 모듈의 하나의 플로우로 통합 |
| core           | 공통 설정·로그 등 기반 코드 (업무 로직 금지) |