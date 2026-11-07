# AWS 목적지 (infra/terraform/aws)

원본이 어디든 AWS를 목적지로 고를 때 쓰는 Terraform입니다. GCP(`../gcp`), Azure(`../../azure`)와 같은 입력·출력 계약을 따릅니다.

| 영역 | 구성 |
| --- | --- |
| 네트워크 | VPC `10.30.0.0/16`, 앱·ALB용 공개 서브넷 2개, DB용 사설 서브넷 2개, 보안 그룹 3개(ALB·앱·DB) |
| 앱 | ECS Fargate 서비스 1개 + ALB(HTTP 80), 그 앞에 CloudFront(https 주소), 상태 확인 `/health/live`, 로그는 CloudWatch |
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
- **앱 주소는 https입니다 (CloudFront).** Worker는 https 목적지만 받습니다(localhost만 예외). ALB의 기본 주소(`….elb.amazonaws.com`)는 AWS 소유 도메인이라 우리가 인증서를 받을 수 없어 https를 직접 붙일 수 없고, CloudFront는 기본 주소(`https://….cloudfront.net`)에 AWS의 인증서가 이미 붙어 있어 도메인 없이도 https가 됩니다. 그래서 `deploy_app = true`일 때 ALB 앞에 CloudFront를 함께 만들고 `app_url` 출력이 그 주소가 됩니다(`enable_https_front`, 기본 켜짐). 사용자 쪽 구간만 https이고 CloudFront에서 ALB까지는 AWS 내부에서 http입니다.
  - 응답은 캐시하지 않고(`Managed-CachingDisabled`) 헤더·쿠키·쿼리는 모두 전달하며(`Managed-AllViewer`) 모든 HTTP 메서드를 받습니다. 원본 응답 대기는 최대 60초입니다.
  - **ALB는 우리 CloudFront를 거친 요청만 받습니다 (2중 장치).**
    1. 보안 그룹: CloudFront 원본 접근용 접두사 목록(`com.amazonaws.global.cloudfront.origin-facing`, AWS가 관리하는 CloudFront 서버 IP 대역 명단)에서 오는 80만 받습니다. ALB 주소로 직접 접속하면 응답 없이 시간 초과됩니다. 이 목록은 보안 그룹 규칙 한도(기본 60)를 약 55개 분량 차지하는 것으로 알려져 있으므로 이 보안 그룹에 규칙을 더 넣지 마세요.
    2. 비밀 헤더: 위 대역은 **모든 AWS 사용자의 CloudFront**가 쓰므로, 다른 사람이 자기 CloudFront에 우리 ALB 주소를 원본으로 적어도 1번을 통과합니다. 그래서 Terraform이 무작위 비밀 값(`random_password.origin_verify`)을 만들고, 우리 CloudFront가 원본 요청마다 `X-Origin-Verify` 헤더로 붙이고, ALB 리스너는 기본 응답을 403으로 두고 이 헤더가 맞는 요청만 앱으로 전달하는 규칙(우선순위 1)을 둡니다. 시청자가 같은 이름의 헤더를 보내도 CloudFront가 설정한 값으로 덮어씁니다(CloudFront 원본 사용자 지정 헤더의 동작으로 알고 있으며 이 환경에서 확인하지는 못했습니다).
    - 비밀 값은 Terraform 상태, CloudFront 배포 설정, ALB 리스너 규칙에 남습니다. 계정 안에서 그 설정을 읽을 수 있는 사람에게는 보이므로 외부 접근을 막는 장치이지 계정 내부자를 막는 장치가 아닙니다. 값을 바꾸려면 `random_password.origin_verify`를 교체하면 되는데, 적용 중 CloudFront와 ALB가 서로 다른 값을 가지는 짧은 순간에는 요청이 403이 될 수 있습니다.
    - 이 장치는 ALB에 우리 CloudFront만 닿게 하는 것이고, **CloudFront 주소 자체가 Worker를 거친 요청만 받게 하는 것은 아닙니다.** 그것(앱이 Worker 헤더를 검사하는 #103 방식)은 AWS에 아직 연결하지 않았습니다.
  - 트래픽 게이트(Worker)의 허용 도메인(`TARGET_ALLOWED_SUFFIXES`)에 `.cloudfront.net`을 넣어야 합니다.
  - 끄는 경우(`enable_https_front = false`): CloudFront를 만들 수 없는 계정이 아니면 끄지 마세요. 앱 주소가 http라서 Worker에 알릴 수 없고, 백엔드는 앱 배포 뒤 Worker에 주소를 알리기 전에 https가 아니면 해결 방법과 함께 멈춥니다. 인증서와 도메인을 ALB에 직접 붙이는 방법은 아직 변수로 열지 않았습니다(후속 과제).
  - CloudFront 배포는 만들 때 전파 시간이 있고 지울 때도 먼저 비활성화해야 해서 `apply`와 `destroy`가 더 오래 걸립니다(소요 시간은 직접 재 보지 못했습니다).
- **VPN**: `vpn_credentials_json`은 null입니다. AWS 목적지로의 CDC 연결(VPN)은 아직 범위 밖입니다.
- **이름 길이**: ALB와 대상 그룹 이름은 32자 제한이라 `service_name`이 32자를 넘으면 실패합니다.
- **비용**: NAT 게이트웨이는 쓰지 않습니다. ALB와 RDS는 켜져 있는 동안 요금이 나옵니다. 시험 뒤에는 `terraform destroy`로 정리하세요(ECR 저장소는 이미지가 있어도 지워집니다).
- **비밀 값**: `secret_refs`는 이름만 만듭니다. 값 이전은 Terraform 밖의 별도 작업이고, **`deploy_app = true` 전에 값을 먼저 넣어야 합니다.** 값이 없는 비밀을 참조하면 ECS 작업이 뜨지 못합니다.
- **상태 확인 경로**: ALB 대상 그룹은 프로세스만 보는 `/health/live`(기본값)를 씁니다. DB까지 보는 `/health/ready`를 쓰면 RDS 재부팅처럼 DB가 잠깐 막힐 때 정상 태스크까지 계속 교체됩니다. 전환 판단용 `/health/ready` 확인은 백엔드가 따로 합니다.
- **DB 접속 허용**: 기본은 앱 보안 그룹에서 오는 5432만 받습니다. CDC가 VPN으로 들어올 때는 원격 대역(예: OCI `10.50.0.0/16`)을 `cdc_allowed_cidrs`로 넣어야 합니다.
- **데모용 설정(운영에서는 위험)**: `skip_final_snapshot = true`, `deletion_protection = false`, ECR `force_delete = true`, 비밀 `recovery_window_in_days = 0`은 `terraform destroy`가 깔끔하도록 둔 값입니다. 운영용으로 쓰기 전에 바꿔야 합니다.
- **직접 접근**: CloudFront를 켜면 ALB 주소로는 직접 접근할 수 없습니다(위 2중 장치). 다만 CloudFront 주소는 공개 주소라서 여기로 직접 가면 Cloudflare Worker를 거치지 않습니다. 앱이 Worker가 붙이는 헤더를 검사하는 방식(GCP의 #72·#103과 같은 방식)은 AWS에 아직 연결하지 않았습니다.
