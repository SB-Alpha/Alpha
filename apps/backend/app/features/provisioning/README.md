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

## DB 등급·디스크 변환 (Azure·AWS)
조사 결과의 `db_tier`는 GCP 등급 이름(`db-g1-small`)이라 Azure·AWS는 받지 못합니다. 그래서 두 목적지의 설정 카드(`azure_adapt`, `aws_adapt`)가 클라우드 중립 값 `db_memory_gib`(원본 DB 메모리)로 등급을 다시 고릅니다.

- 규칙: 원본 메모리 **이상인 가장 작은 등급** (GCP의 `_db_tier`와 같은 규칙). 메모리를 모르면 `db_vcpu`로, 둘 다 모르면 등급을 넘기지 않아 Terraform 기본값(Azure `B_Standard_B1ms`, AWS `db.t4g.small`)을 씁니다. 표의 가장 큰 등급으로도 모자라면 그 등급으로 맞추고 안내합니다.
- Azure 등급: `B_Standard_B1ms`(2 GiB), `B2s`(4), `B2ms`(8), `GP_Standard_D4ds_v5`(16), `D8ds_v5`(32), `D16ds_v5`(64), `D32ds_v5`(128). AWS 등급: `db.t4g.micro`(1 GiB)~`2xlarge`(32), `db.m6g.4xlarge`(64), `8xlarge`(128). 이름과 크기는 2026-11-08에 `az postgres flexible-server list-skus`와 `aws rds`·`aws ec2 describe-instance-types`로 직접 조회한 값이며, 새 등급을 넣을 때도 같은 명령으로 확인합니다.
- 디스크: Azure는 허용 크기(32, 64, 128 ... 32768)로 올림, AWS는 최소 20 GB(gp3) 이상으로 맞춥니다. 크기를 모르면 Terraform 기본값입니다.
- Azure는 최대 연결 수가 서버를 만들 때 등급으로 정해지므로, 구축 단계와 앱 배포 단계에서 같은 등급이 나와야 합니다(같은 입력이면 같은 결과).
- 한계: 조사 쪽 RDS 클래스 메모리 표(`RDS_MEMORY_GIB`)에 지금 `micro`·`small`만 있어서, 그보다 큰 원본은 `db_memory_gib`를 모르는 값으로 두고 기본 등급을 씁니다. HA, 고가용성, 읽기 복제본은 변환하지 않습니다.

## 미정 사항 (합의 필요)
- Terraform 실행 방식 (CLI 호출 등)과 상태 저장 위치
- 장시간 작업의 상태 보고 방식
- 담당자
