# AWS 목적지 (infra/terraform/aws)

원본이 어디든 AWS를 목적지로 고를 때 쓰는 Terraform입니다. GCP(`../gcp`), Azure(`../../azure`)와 같은 입력·출력 계약을 따릅니다.

| 영역 | 구성 |
| --- | --- |
| 네트워크 | VPC `10.30.0.0/16`, 앱·ALB용 공개 서브넷 2개, DB용 사설 서브넷 2개, 보안 그룹 3개(ALB·앱·DB) |
| 앱 | ECS Fargate 서비스 1개 + ALB(HTTP 80), 상태 확인 `/health/ready`, 로그는 CloudWatch |
| DB | RDS PostgreSQL 16, 논리 복제(`rds.logical_replication`)와 암호화 접속 강제(`rds.force_ssl`), 사설 접속만 |
| 이미지·비밀 | ECR(저장소 이름 = 서비스 이름), Secrets Manager, ECS 실행 역할 |

## 실행 방식
- 인프라와 앱을 두 번에 나눠 apply합니다. 첫 apply는 `deploy_app = false`(기본값)로 앱 없이 인프라만 만들고, DB 이주가 streaming에 들어가 테이블이 만들어진 뒤 `deploy_app = true`로 두 번째 apply를 합니다. 그 사이에 백엔드가 이미지를 ECR(`<registry_url>/<service_name>`)로 복사하고 digest로 고정한 주소를 `container_image`로 넘깁니다.
- 상태는 HCP Terraform의 `team-alpha` 조직, `alpha-aws-destination` 워크스페이스(**Local 실행 모드**)에 둡니다.
- 로그인은 AWS 기본 자격 증명(환경변수, 프로필, 역할)을 씁니다. 엉뚱한 계정에 만드는 사고를 막으려면 `aws_account_id`를 넘기세요. 그 계정이 아니면 plan부터 거부됩니다. 백엔드는 환경변수 `AWS_ACCOUNT_ID`를 읽어 넘깁니다.

## 알아 둘 점
- **DB 사용자**: RDS는 Terraform만으로 사용자를 더 만들 수 없어서 `db_user`(기본 `alpha`) 마스터 하나가 앱과 관리자(db_migration)를 겸합니다. 앱 전용 사용자 분리는 후속 작업입니다.
- **`db_private_ip`는 IP가 아니라 호스트 이름입니다.** 이름은 GCP·Azure와 계약을 맞추려고 그대로 두었고, RDS에 고정 IP가 없어 엔드포인트 호스트 이름이 들어갑니다(VPC 안에서는 사설 IP로 풀립니다). 백엔드는 이 값을 접속 주소로만 쓰며 IP 형식을 검사하지 않습니다. `cloud_sql_private_ip` 지표만 IP일 때 보고됩니다.
- **`db_fqdn`** 은 같은 값이고 백엔드는 읽지 않습니다(Azure 구성과 이름만 맞춘 참고용).
- **앱 주소는 HTTP**입니다. 인증서와 도메인이 없어서 ALB 80 포트만 엽니다. 트래픽 게이트(Worker)가 이 주소로 보내려면 허용 도메인(`TARGET_ALLOWED_SUFFIXES`)에 `.elb.amazonaws.com`을 넣어야 합니다.
- **VPN**: `vpn_credentials_json`은 null입니다. AWS 목적지로의 CDC 연결(VPN)은 아직 범위 밖입니다.
- **이름 길이**: ALB와 대상 그룹 이름은 32자 제한이라 `service_name`이 32자를 넘으면 실패합니다.
- **비용**: NAT 게이트웨이는 쓰지 않습니다. ALB와 RDS는 켜져 있는 동안 요금이 나옵니다. 시험 뒤에는 `terraform destroy`로 정리하세요(ECR 저장소는 이미지가 있어도 지워집니다).
- **비밀 값**: `secret_refs`는 이름만 만듭니다. 값 이전은 Terraform 밖의 별도 작업이고, **`deploy_app = true` 전에 값을 먼저 넣어야 합니다.** 값이 없는 비밀을 참조하면 ECS 작업이 뜨지 못합니다.
- **상태 확인 경로**: ALB 대상 그룹은 프로세스만 보는 `/health/live`(기본값)를 씁니다. DB까지 보는 `/health/ready`를 쓰면 RDS 재부팅처럼 DB가 잠깐 막힐 때 정상 태스크까지 계속 교체됩니다. 전환 판단용 `/health/ready` 확인은 백엔드가 따로 합니다.
- **DB 접속 허용**: 기본은 앱 보안 그룹에서 오는 5432만 받습니다. CDC가 VPN으로 들어올 때는 원격 대역(예: OCI `10.50.0.0/16`)을 `cdc_allowed_cidrs`로 넣어야 합니다.
- **데모용 설정(운영에서는 위험)**: `skip_final_snapshot = true`, `deletion_protection = false`, ECR `force_delete = true`, 비밀 `recovery_window_in_days = 0`은 `terraform destroy`가 깔끔하도록 둔 값입니다. 운영용으로 쓰기 전에 바꿔야 합니다.
- **ALB 직접 접근**: ALB가 HTTP 80을 인터넷에 열어 두어서 ALB 주소로 직접 가면 Cloudflare Worker를 거치지 않습니다. 앞단 우회 차단(GCP의 #72와 같은 방식)은 후속 작업입니다.
