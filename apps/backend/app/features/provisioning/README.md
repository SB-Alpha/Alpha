# provisioning

## 책임
목적지(GCP) 앱·DB 실행에 필요한 인프라를 구축하고 결과를 반환합니다.
`infra/terraform/gcp/`의 IaC를 실행하는 모듈입니다.

## 예상 입력
- 목적지 설계안 (구성 자원·사양)
- GCP 자격증명 참조

## 예상 출력
- 생성된 자원 정보 (엔드포인트·식별자 등)
- 실행 상태·오류

## 이미지 복사 (app_deploy)
앱을 만들기 전에 원본 이미지를 목적지 저장소(`<registry_url>/<service_name>`)로 복사하고 digest 로 고정한 주소를
`container_image` 로 넘깁니다. 코드는 `image_transfer.py` 와 `service.py` 의 `_image_step` 입니다.

| 원본 | 동작 |
| --- | --- |
| AWS ECR | 원본 AWS 키(`source_credentials_ref`, `secret://...`)로 로그인해 복사합니다. 키가 없거나 `secret://` 형식이 아니면(로컬 `profile:` 등) 복사를 건너뛰고 목적지 저장소에 이미지가 이미 있다고 봅니다. |
| 공개 저장소 (Docker Hub 의 `nginx:1.27`·`team/app` 포함, 로그인이 필요 없는 저장소) | 로그인 없이 복사합니다. 저장소 주소가 없는 이름은 Docker Hub(`index.docker.io`)로 읽습니다. |
| 로그인이 필요한 ECR 외 비공개 저장소 | **자동 복사하지 않습니다 (한계).** 그 저장소 열쇠를 받을 입력이 없습니다(`assessment` 계약이 바뀌어야 함). 복사에 실패하면 목적지에 같은 이름의 이미지가 이미 있는지 보고, 있으면 그것을 씁니다. 없으면 앱을 만들기 전에 멈추고 `crane copy <원본> <목적지>` 를 안내하니, 사람이 복사한 뒤 같은 작업을 다시 실행하면 이어집니다. |

- 복사하지 않는 경우: 원본 이미지가 없을 때, `container_image` 가 이미 지정됐을 때, `dry_run` 일 때.
- 실행 환경에는 `crane`(`brew install crane`, 또는 `CRANE_BIN`)과 목적지 로그인이 필요합니다. 목적지 로그인은 GCP(Artifact Registry, ADC), Azure(ACR, `az login` 신분의 `az acr login --expose-token` 토큰, `az` CLI 필요), AWS(ECR, 실행 환경의 AWS 로그인. `AWS_PROFILE` 등)를 지원합니다. 목적지 저장소 주소(`registry_url`)는 GCP처럼 `호스트/프로젝트/저장소`여도, Azure·AWS처럼 호스트만이어도 됩니다. 이미지는 어느 쪽이든 `<registry_url>/<service_name>`에 올라갑니다. 그 밖의 목적지는 복사를 건너뜁니다.
- Azure는 역할(AcrPush) 부여 직후 push가 권한 오류로 실패할 수 있어 최대 4번(20초 간격) 다시 시도합니다. DR 저장소가 있으면(`dr_registry_url`) 같은 이미지를 그곳에도 복사합니다.
- Azure 실제 push는 아직 시험하지 못했습니다(단위 테스트만).
- 복사 후 digest 가 원본에 지정된 digest 와 다르면 실패합니다(사람이 목적지에 잘못된 이미지를 넣은 경우도 같습니다).

## 미정 사항 (합의 필요)
- Terraform 실행 방식 (CLI 호출 등)과 상태 저장 위치
- 장시간 작업의 상태 보고 방식
- 담당자
