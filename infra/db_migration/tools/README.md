# CDC 실제 입력 준비

001의 `POST /db-migration/runs` 요청 JSON과 백엔드 DB에 넣을 권한·계정 값을 준비한다. 현재 도구는 **AWS 원본 → GCP 목적지**용이다. 003~006의 6개 경로 지원을 완료한 도구는 아니다. 클라우드 API는 조회만 하며 VPN, DB 접속, 복제, 전환은 실행하지 않는다.

레포 루트에서 실행한다. Backend 의존성이 설치된 Python 3.11 이상이 필요하다.

```bash
bash infra/db_migration/tools/prepare-inputs.sh
```

기본 실행에서 입력할 세 값:

1. AWS RDS 리전. `--source-region`으로 주면 묻지 않는다.
2. GCP 서비스 계정 키 JSON **파일 경로**. `credentials_json`에는 파일 내용 전체가 들어간다.
3. GCP 목적지 DB 관리자 비밀번호. 터미널에 표시되지 않는다.

원본 DB 계정은 같은 백엔드의 비밀 저장소에서 재사용한다. 후보가 하나면 선택하고 참조 이름을 표시하며, 여러 개면 `--source-run-id`를 요구한다. AWS 권한은 `cloud/aws`, 선택한 실행의 `source-cloud` 순서로 찾고, 없으면 AWS 기본 프로필을 쓴다. `--aws-profile`은 지정한 프로필을 우선한다. 임시 AWS 세션 키는 001에 따라 거절한다.

GCP 리전은 프로젝트의 primary PostgreSQL이 하나면 조회한다. 여러 리전에 있으면 `--target-region`을 지정하고, 해당 리전에서 인스턴스가 정확히 하나인지 검사한다. 목적지 관리자 기본값 `postgres`와 앱 계정 기본값 `alpha`는 현재 대상 Terraform의 구성에 맞춘 입력 기본값이다. 기존 계정 존재나 비밀번호가 검증됐다는 뜻은 아니다. 다른 계정은 `--target-user`, `--target-app-user`로 지정한다.

```bash
bash infra/db_migration/tools/prepare-inputs.sh \
  --source-run-id mig-existing \
  --source-region ap-northeast-2 \
  --target-region asia-northeast3
```

키 파일을 옵션으로 지정하고 원본 DB 비밀번호도 직접 입력하려면 아래처럼 실행한다. 이때 숨김 입력 두 번은 **원본 DB 비밀번호, 목적지 DB 비밀번호**다.

```bash
bash infra/db_migration/tools/prepare-inputs.sh \
  --gcp-key /absolute/path/service-account.json \
  --source-user alpha \
  --aws-profile alpha-cdc
```

가상환경이 다른 곳에 있으면 `CDC_INPUTS_PYTHON=/absolute/path/venv/bin/python`을 앞에 붙인다. 명령행 옵션으로 비밀번호나 키 원문을 받지 않는다.

조회 완료 뒤:

- **Enter**: 001 요청 JSON을 클립보드에 복사한다.
- **d + Enter**: DB 저장용 이름 → 값 매핑을 복사한다. `cloud/gcp.credentials_json`도 포함한다.
- **b + Enter**: 요청·DB 저장값·조회한 DB 주소와 네트워크·검증 상태 전체를 복사한다.
- **q + Enter**: 종료한다.

실제 값은 `apps/backend/.data/cdc-inputs/cdc-inputs-*.json`에도 저장한다. 디렉터리는 새로 만들 때 `700`, 파일은 `600`이며 기존 `.data/` Git 제외 규칙이 적용된다. 이 파일과 클립보드에는 비밀번호와 클라우드 키가 들어 있으므로 Notion·채팅·Git에 붙여 넣지 않는다. 클립보드 도구가 없으면 복사 실패를 알리고 저장 파일을 남긴다. `--no-clipboard`는 파일 저장 후 종료한다.

DB 저장값 JSON은 등록 전 원문을 준비한 자료다. SQLite의 암호문 `value` 컬럼에 직접 넣는 SQL이 아니다. 실제 등록은 아래 옵션으로 기존 `put_secret`과 `inject`를 사용한다.

```bash
bash infra/db_migration/tools/prepare-inputs.sh --register
```

Backend와 **동일한 `ALPHA_DATA_DIR`, `ALPHA_SECRETS_KEY` 또는 OS 키체인**을 사용해야 한다. 같은 이름의 값은 덮어쓴다. 등록 이름은 `cloud/aws`, `cloud/gcp`, `{run_id}/source-db-admin`, `{run_id}/target-admin`, `{run_id}/run-clouds`이며, DB에는 암호화되어 저장된다. 등록해도 실행 상태를 만들거나 이전을 시작하지 않는다. 생성한 요청 JSON은 별도로 `/db-migration/runs`에 전달한다.

GCP 키나 Cloud SQL 조회 권한으로 기존 DB 비밀번호는 읽을 수 없다. 도구의 `discovered` 값은 실제 API 조회 결과이고 비밀번호는 사용자가 준 값 또는 기존 저장값이다. 비밀번호·목적지 앱 계정·원본 앱의 계정 일치·사용자 DB와 테이블·VPN 및 시작 조건은 미검증으로 기록한다. 이 검증은 001의 VPN 이후 단계에서 수행한다. 원본에 저장된 값이 예전 시험용 값이면 갱신해야 하며, 조회 실패를 가짜 값으로 대체하지 않는다.
