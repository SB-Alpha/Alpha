# terraform/gcp

GCP 목적지 앱·DB 인프라를 생성하는 Terraform 코드 자리입니다. `provisioning` 모듈이 실행합니다.

- 자원 정의는 아직 작성하지 않았습니다.
- CDC 인프라(`infra/db_migration/terraform/`)와 상태를 공유하지 않습니다.

## 직접 호출 차단 (`protect_ingress`, 이슈 #34)
`protect_ingress=true` 이면 `worker_secret`(`TF_VAR_worker_secret` 로 넘김, 민감 값)을 Secret Manager 에 `<name>-worker-secret` 으로 저장하고, 서비스 계정에 접근 권한을 주며, Cloud Run 에 `WORKER_SECRET`(비밀 환경 변수)과 `REQUIRE_WORKER_SECRET=true` 를 넣습니다. 앱이 `X-Alpha-Worker-Secret` 헤더를 검사해 Worker 를 거치지 않은 호출을 403 으로 막는 용도입니다. Cloud Run 자체는 Worker 가 인터넷에서 부르므로 `allUsers` 호출을 그대로 허용합니다. 출력 `ingress_guard` 는 `worker_secret` 또는 `open` 입니다.

## 미정 사항 (합의 필요)
- 목적지 자원 구성 (앱 실행 환경, DB 종류 등)
- 원격 상태(backend) 위치
- 변수·출력 규격 (`provisioning` 계약과 연결)
