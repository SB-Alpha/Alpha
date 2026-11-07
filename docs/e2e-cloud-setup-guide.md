# Cloudmorph E2E 클라우드 환경 구축 및 마이그레이션 실행 매뉴얼
> **AWS & GCP 계정/결제 생성부터 IAM 키 발급, 대상 리소스 배포, 플랫폼 시크릿 등록 및 UI 실행까지 A to Z 완벽 가이드**

---

## 📌 목차
1. [전체 아키텍처 및 입력 필드 매핑 총괄](#1-전체-아키텍처-및-입력-필드-매핑-총괄)
2. [Phase 1: AWS 환경 구축 (계정, 결제, IAM, 키 발급)](#2-phase-1-aws-환경-구축-계정-결제-iam-키-발급)
   - 2.1 AWS 계정 가입 및 결제 카드 등록 (Free Tier)
   - 2.2 IAM 사용자(`cloudmorph-migrator`) 생성 및 정책(JSON) 부여
   - 2.3 Access Key 발급 및 로컬 AWS CLI 설정
   - 2.4 이전 대상 소스 리소스 생성 (RDS PostgreSQL 16 & ECS Fargate)
3. [Phase 2: GCP 환경 구축 (계정, 무료 크레딧, 프로젝트, SA 키 발급)](#3-phase-2-gcp-환경-구축-계정-무료-크레딧-프로젝트-sa-키-발급)
   - 3.1 GCP 가입 및 결제 계정 연결 ($300 무료 크레딧)
   - 3.2 프로젝트 생성 및 필수 API 일괄 활성화
   - 3.3 Terraform / 프로비저닝용 서비스 계정(SA) 생성 및 권한 부여
   - 3.4 서비스 계정 키(JSON) 발급 및 로컬 환경변수 설정
4. [Phase 3: 플랫폼 시크릿 스토어(`secret://`) 등록](#4-phase-3-플랫폼-시크릿-스토어secret-등록)
   - 4.1 시크릿 규격 및 태그 정의
   - 4.2 원클릭 Python 등록 스크립트
5. [Phase 4: UI 입력 폼 완성 및 마이그레이션 실행 매뉴얼](#5-phase-4-ui-입력-폼-완성-및-마이그레이션-실행-매뉴얼)
   - 5.1 UI 필드별 실제 입력값 예시표
   - 5.2 6단계 라이프사이클 진행 및 승인 게이트 조작
6. [Phase 5: 비용 관리 및 실습 후 리소스 삭제 (Clean-up)](#6-phase-5-비용-관리-및-실습-후-리소스-삭제-clean-up)

---

## 1. 전체 아키텍처 및 입력 필드 매핑 총괄

Cloudmorph 플랫폼은 **AWS(ECS Fargate + RDS PostgreSQL)** 환경의 소스 워크로드를 **GCP(Cloud Run + Cloud SQL PostgreSQL)** 환경으로 **무중단(Zero-Downtime)** 이전합니다.

```
[Source: AWS (ap-northeast-2)]                [Cloudmorph Platform]                 [Target: GCP (asia-northeast3)]
┌─────────────────────────────────┐      ┌─────────────────────────────┐      ┌──────────────────────────────────┐
│ ECS Fargate (target-app)        │      │ 1. Discovery & Planning     │      │ Cloud Run (target-app)           │
│   - Port: 8080                  │ ───> │    (Assessment Module)      │ ───> │   - VPC Connector                │
│   - Health: /health/ready       │      │ 2. 승인 ① (Plan Approval)  │      │   - Health: /health/ready        │
│ RDS PostgreSQL 16 (target-db)   │      │ 3. Terraform Provisioning   │      │ Cloud SQL PostgreSQL 16          │
│   - logical_replication = 1     │ ───> │ 4. CDC DB Migration (Kafka) │ ───> │   - Private IP (VPC Peering)     │
│   - Table PK / UUID 필수        │      │ 5. 승인 ② (Cutover Approval)│      │   - Logical Decoding 활성화      │
└─────────────────────────────────┘      │ 6. Cloudflare Traffic Switch│      └──────────────────────────────────┘
                                         └─────────────────────────────┘
```

### UI 입력 필드와 백엔드 모듈 매핑
UI(`http://localhost:3000`)의 입력 항목입니다. 위쪽 4개만 채우면 시작할 수 있고, 나머지는 "고급 클라우드 설정"에 있습니다.

| UI 입력 항목 | 필드명 | 필수 | 형식 / 예시값 | 설명 및 사용처 |
| :--- | :--- | :---: | :--- | :--- |
| **서비스 이름** | `service_name` | ✅ | `target-app` | 원본 서비스 이름. ECS 서비스를 따로 적지 않으면 이 이름을 씁니다 |
| **원본 AWS 자격증명 참조** | `aws_credentials_ref` | ✅ | `secret://mig/source-cloud` | 소스 리소스(ECS, RDS) 인벤토리 조회용 AWS 인증 정보 |
| **원본 DB 관리자 참조** | `db_admin_ref` | ✅ (DB 접속 정보 참조가 있으면 선택) | `secret://mig/source-db-admin` | CDC 복제를 위한 소스 PostgreSQL 관리자(superuser) 계정 |
| **목적지 클라우드** | `target_infra.provider` | ✅ | GCP | 어댑터가 등록된 클라우드만 고를 수 있습니다 (`docs/TARGET_ADAPTER_SPEC.md`) |
| **원본 AWS 리전** | `aws_region` | 고급 | `ap-northeast-2` | 소스 ECS, RDS, VPC가 위치한 AWS 리전 |
| **AWS 조회 역할 ARN** | `aws_role_ref` | 고급 | `arn:aws:iam::123456789012:role/CloudmorphReader` | 크로스 계정 조회 시 STS AssumeRole 대상 역할 |
| **ECS 서비스** | `ecs_service` | 고급 | `target-app` | 서비스 이름과 다를 때만 적습니다 |
| **RDS 식별자** | `rds_instance_id` | 고급 | `target-db` | 비우면 계정의 RDS 가 하나뿐일 때 자동으로 고릅니다. 여러 개면 적어야 합니다 |
| **DB 접속 정보 참조** | `db_credentials_ref` | 고급 | `arn:aws:secretsmanager:...` | AWS Secrets Manager에 저장된 원본 DB 계정 ARN |
| **GitHub 저장소** | `github_repo_url` | 고급 | `https://github.com/Jeonsubb/cloudmorph-demo-guestbook` | 선택. 이미지는 provisioning 이 원본 ECR 에서 복사하므로 없어도 됩니다 |
| **목적지 리전** | `target_region` | 고급 | `asia-northeast3` | 비우면 목적지 클라우드의 기본 리전(서울)을 씁니다 |

`profile:` 참조는 로컬 CLI 전용이라 화면에서 시작한 run 에서는 쓰지 않습니다.

---

## 2. Phase 1: AWS 환경 구축 (계정, 결제, IAM, 키 발급)

### 2.1 AWS 계정 가입 및 결제 카드 등록 (Free Tier)
1. [AWS 공식 웹사이트](https://aws.amazon.com/ko/)에서 **"AWS 계정 생성"** 클릭.
2. 이메일, 계정 이름 입력 후 해외 결제 가능한 신용/체크카드(VISA, Master, AMEX 등)를 등록합니다.
   - *$1 가결제 후 즉시 환불 또는 취소 처리됩니다.*
3. 본인 인증(SMS/전화) 완료 후 **"기본 지원 - 무료 (Basic Support)"** 플랜을 선택합니다.
4. 가입 직후 12개월간 EC2, RDS, S3 등 AWS Free Tier 혜택이 적용됩니다.

---

### 2.2 IAM 사용자(`cloudmorph-migrator`) 생성 및 정책 부여
루트 계정 대신 마이그레이션 전용 최소 권한을 가진 IAM 사용자를 생성합니다.

1. AWS 콘솔 상단 검색창에 **IAM** 입력 후 진입.
2. 좌측 메뉴 **[사용자(Users)]** ➔ **[사용자 생성(Create user)]** 클릭.
3. 사용자 이름: `cloudmorph-migrator` 입력 후 [다음] 클릭.
4. **권한 설정**: **[직접 정책 연결(Attach policies directly)]** ➔ **[정책 생성(Create policy)]** 클릭.
5. 정책 편집기에서 **JSON** 탭을 선택하고 아래 정책을 붙여넣습니다:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudmorphAssessmentReadOnly",
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeServices",
        "ecs:DescribeTaskDefinition",
        "ecs:DescribeClusters",
        "ecs:ListTasks",
        "ecs:DescribeTasks",
        "rds:DescribeDBInstances",
        "rds:DescribeDBSubnetGroups",
        "rds:DescribeDBParameters",
        "ec2:DescribeVpcs",
        "ec2:DescribeSubnets",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeRouteTables",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret"
      ],
      "Resource": "*"
    },
    {
      "Sid": "AllowSTSAssumeRoleIfConfigured",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "*"
    }
  ]
}
```
6. 정책 이름: `CloudmorphAssessmentPolicy`로 저장한 뒤, 사용자 생성 화면으로 돌아와 새로고침 후 해당 정책을 체크하고 사용자를 생성합니다.

---

### 2.3 Access Key 발급 및 로컬 AWS CLI 설정
1. 생성된 `cloudmorph-migrator` 사용자 클릭 ➔ **[보안 자격 증명(Security credentials)]** 탭 진입.
2. **액세스 키(Access keys)** 섹션 ➔ **[액세스 키 만들기(Create access key)]** 클릭.
3. 사용 사례: **"Command Line Interface (CLI)"** 선택 ➔ 확인 체크 후 생성.
4. **Access Key ID**와 **Secret Access Key**를 안전한 메모장에 복사해 둡니다 (CSV 다운로드 권장).

#### 로컬 PC에 AWS CLI 프로필 등록 (터미널)
```bash
# AWS CLI 가 설치되어 있지 않다면 (macOS)
brew install awscli

# AWS 프로필 등록 (default 프로필로 등록)
aws configure --profile default
# AWS Access Key ID [None]: <발급받은 Access Key ID 붙여넣기>
# AWS Secret Access Key [None]: <발급받은 Secret Access Key 붙여넣기>
# Default region name [None]: ap-northeast-2
# Default output format [None]: json

# 정상 연결 테스트
aws sts get-caller-identity --profile default
```
> **정상 결과 확인:**
> `{"UserId": "AIDAX...", "Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/cloudmorph-migrator"}`

---

### 2.4 이전 대상 소스 리소스 생성 (RDS PostgreSQL 16 & ECS Fargate)
평가 및 CDC 마이그레이션의 대상이 될 원본 리소스를 준비합니다.

#### 1) RDS PostgreSQL 16 파라미터 그룹 및 인스턴스 생성
> ⚠️ **매우 중요:** CDC 논리 복제를 위해 파라미터 그룹에서 `rds.logical_replication=1` 설정이 필수입니다!

```bash
# 1. 논리 복제 활성화 파라미터 그룹 생성
aws rds create-db-parameter-group \
  --db-parameter-group-name pg16-cdc-group \
  --db-parameter-group-family postgres16 \
  --description "Parameter group for PostgreSQL 16 with CDC enabled" \
  --region ap-northeast-2

# 2. logical_replication 활성화 (재부팅 필요)
aws rds modify-db-parameter-group \
  --db-parameter-group-name pg16-cdc-group \
  --parameters "ParameterName=rds.logical_replication,ParameterValue=1,ApplyMethod=pending-reboot" \
  --region ap-northeast-2

# 3. RDS 인스턴스 생성 (테스트용 프리티어 db.t3.micro 또는 db.t4g.micro)
# 비밀번호는 직접 정해 환경변수로만 둡니다. 공개 저장소이므로 문서나 코드에 적지 마세요.
read -rs SOURCE_DB_PASSWORD && export SOURCE_DB_PASSWORD
aws rds create-db-instance \
  --db-instance-identifier target-db \
  --db-instance-class db.t4g.micro \
  --engine postgres \
  --engine-version 16.3 \
  --master-username postgres \
  --master-user-password "$SOURCE_DB_PASSWORD" \
  --allocated-storage 20 \
  --db-name app_db \
  --db-parameter-group-name pg16-cdc-group \
  --no-publicly-accessible \
  --region ap-northeast-2
```

> 원본 RDS 는 퍼블릭 액세스를 끕니다. assessment 는 bastion(SSM 터널)으로 접속하고, bastion 이 없으면 DB 조회를 건너뛰어 gaps 에 남깁니다 (`apps/backend/app/features/assessment/README.md`).

#### 2) ECS 클러스터 및 Fargate 서비스(`target-app`) 생성
```bash
# 1. ECS 클러스터 생성
aws ecs create-cluster --cluster-name target-cluster --region ap-northeast-2

# 2. 작업 정의 등록 (samples/target-app 기반 컨테이너)
aws ecs register-task-definition \
  --family target-app-task \
  --network-mode awsvpc \
  --requires-compatibilities FARGATE \
  --cpu "256" \
  --memory "512" \
  --container-definitions '[{
    "name": "target-app",
    "image": "public.ecr.aws/docker/library/python:3.12-slim",
    "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
    "essential": true,
    "command": ["python3", "-m", "http.server", "8080"]
  }]' \
  --region ap-northeast-2
```

---

## 3. Phase 2: GCP 환경 구축 (계정, 무료 크레딧, 프로젝트, SA 키 발급)

### 3.1 GCP 가입 및 결제 계정 연결 ($300 무료 크레딧)
1. [Google Cloud Console](https://console.cloud.google.com/) 접속 및 Google 계정 로그인.
2. 상단 배너의 **"무료 평가판 시작 ($300 무료 크레딧)"** 클릭.
3. 국가(대한민국) 선택, 개인/사업자 정보 및 결제 수단(신용/체크카드) 입력.
   - *평가판 기간 동안 $300 크레딧이 먼저 소진되며, 무료 한도 초과 시 자동 청구되지 않도록 수동 업그레이드가 기본 차단되어 안전합니다.*

---

### 3.2 프로젝트 생성 및 필수 API 일괄 활성화
터미널에서 `gcloud` CLI를 사용해 원클릭으로 세팅합니다.

```bash
# Google Cloud SDK 설치 (macOS)
brew install --cask google-cloud-sdk

# gcloud 로그인
gcloud auth login

# 1. 새 GCP 프로젝트 생성 (전 세계 유일한 ID 지정)
export GCP_PROJECT_ID="cloudmorph-alpha-$(date +%s)"
echo "생성할 프로젝트 ID: $GCP_PROJECT_ID"
gcloud projects create $GCP_PROJECT_ID --name="Cloudmorph Alpha"

# 2. 생성한 프로젝트를 기본 프로젝트로 설정
gcloud config set project $GCP_PROJECT_ID

# 3. 결제 계정(Billing Account) 연결
# 결제 계정 ID 확인:
gcloud billing accounts list
# ACCOUNT_ID 확인 후 연결 (예: 01ABCD-2345EF-678901)
export BILLING_ACCOUNT_ID=$(gcloud billing accounts list --format="value(ACCOUNT_ID)" | head -n 1)
gcloud billing projects link $GCP_PROJECT_ID --billing-account=$BILLING_ACCOUNT_ID

# 4. Terraform 프로비저닝에 필요한 6대 필수 API 일괄 활성화
gcloud services enable \
  compute.googleapis.com \
  sqladmin.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  servicenetworking.googleapis.com
```

---

### 3.3 Terraform / 프로비저닝용 서비스 계정(SA) 생성 및 권한 부여
플랫폼이 Terraform(`infra/terraform/gcp`)을 통해 VPC, Cloud SQL, Artifact Registry, Cloud Run, Secret Manager를 자동으로 생성할 수 있도록 서비스 계정을 생성합니다.

```bash
# 1. 서비스 계정 생성
gcloud iam service-accounts create terraform-runner \
  --description="Service account for Cloudmorph Terraform Provisioning" \
  --display-name="Terraform Runner"

export SA_EMAIL="terraform-runner@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# 2. 필수 역할(Roles) 일괄 부여
for ROLE in \
  roles/compute.networkAdmin \
  roles/cloudsql.admin \
  roles/run.admin \
  roles/artifactregistry.admin \
  roles/secretmanager.admin \
  roles/iam.serviceAccountUser \
  roles/resourcemanager.projectIamAdmin
do
  gcloud projects add-iam-policy-binding $GCP_PROJECT_ID \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$ROLE"
done
```

---

### 3.4 서비스 계정 키(JSON) 발급 및 로컬 환경변수 설정
```bash
# 1. 서비스 계정 키 발급 및 저장
mkdir -p ~/.config/gcloud
gcloud iam service-accounts keys create ~/.config/gcloud/cloudmorph-sa-key.json \
  --iam-account="${SA_EMAIL}"

# 2. Application Default Credentials (ADC) 동기화
gcloud auth application-default login

# 3. 백엔드 .env 파일에 GCP 프로젝트 설정 추가
cd /Users/pj/dev/softbank-cloudmorph-next/apps/backend
echo "GCP_PROJECT_ID=${GCP_PROJECT_ID}" >> .env
echo "GOOGLE_APPLICATION_CREDENTIALS=${HOME}/.config/gcloud/cloudmorph-sa-key.json" >> .env
echo "ALPHA_ALLOW_AWS_PROFILE=1" >> .env
```

---

## 4. Phase 3: 플랫폼 시크릿 스토어(`secret://`) 등록

Cloudmorph 백엔드는 보안을 위해 평문 비밀번호나 민감한 클라우드 키를 데이터베이스나 코드에 남기지 않고, **로컬 SQLite에 Fernet AES-128로 암호화(`app.core.secrets`)**하여 보관하며 오직 `secret://...` 참조 이름표로만 주고받습니다.

### 4.1 시크릿 태그 및 필수 키 규격

| 태그 (`tag`) | 필수 키 (`required keys`) | 선택 키 (`optional`) | 설명 |
| :--- | :--- | :--- | :--- |
| **`source-cloud`** | `aws_access_key_id`, `aws_secret_access_key` | `aws_session_token` | AWS IAM 자격증명 |
| **`source-db-admin`** | `user`, `password` | - | 원본 RDS PostgreSQL superuser 계정 |
| **`target-cloud`** | `credentials_json` | - | GCP 서비스 계정 키 JSON 문자열 |
| **`target-admin`** | `user`, `password` | - | 대상 Cloud SQL PostgreSQL 초기 관리자 비밀번호 |

---

### 4.2 원클릭 Python 등록 스크립트
`apps/backend/scripts/register_secrets.py` 로 테스트용 공용 참조(`secret://default/...`)를 백엔드 암호화 스토어에 등록합니다. 실행 전용 참조는 `--run-id mig` 처럼 접두어를 줍니다.

값은 인자, 환경변수 순으로 읽고, 없으면 터미널에서 묻습니다. 터미널이 아니면 아무것도 저장하지 않고 오류로 끝납니다.

| 값 | 환경변수 |
| :--- | :--- |
| 원본 RDS 비밀번호 (2.4 에서 정한 값) | `SOURCE_DB_PASSWORD` |
| 대상 Cloud SQL 초기 관리자 비밀번호 | `TARGET_DB_PASSWORD` |
| AWS 키 | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |
| GCP 서비스 계정 키 파일 경로 (기본 `~/.config/gcloud/cloudmorph-sa-key.json`) | `GOOGLE_APPLICATION_CREDENTIALS` |

터미널에서 바로 실행 (저장소 루트 기준):
```bash
cd apps/backend
uv run python scripts/register_secrets.py
```

---

## 5. Phase 4: UI 입력 폼 완성 및 마이그레이션 실행 매뉴얼

### 5.1 UI 필드별 실제 입력값 예시표
브라우저에서 `http://localhost:3000` 접속 후 아래와 같이 입력합니다:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        새 이전 요청 등록                                 │
├────────────────────────────────────────────────────────────────────────┤
│ [필수 항목]                                                            │
│ GitHub 저장소       : https://github.com/Jeonsubb/cloudmorph-demo-guestbook │
│ 원본 AWS 리전       : ap-northeast-2                                   │
│ 목적지 GCP 리전      : asia-northeast3                                  │
│                                                                        │
│ [참조 및 자원 항목]                                                    │
│ AWS 자격증명 참조   : profile:default   (또는 secret://default/source-cloud)│
│ AWS 조회 역할 ARN   : (비워둠 - 필요 시 arn:aws:iam::... 입력)           │
│ ECS 서비스          : target-app                                       │
│ RDS 식별자          : target-db                                        │
│ DB 접속 정보 참조   : (비워둠)                                          │
│ DB 관리자 계정 참조 : secret://default/source-db-admin                 │
└────────────────────────────────────────────────────────────────────────┘
```

---

### 5.2 6단계 라이프사이클 진행 및 승인 게이트 조작

1. **[이전 요청 생성] 버튼 클릭**
   - 즉시 `POST /runs`가 호출되며 고유 Run ID(`mig-YYYYMMDD-xxxxxxxx`)가 발급되고 상세 화면(`/runs/[id]`)으로 자동 이동합니다.
2. **Discovery & Planning 자동 수행**
   - 백그라운드 엔진이 AWS 소스 환경을 스캔하고 대상 GCP 인프라 사양(1 vCPU, 512MiB Cloud Run + Cloud SQL PG 16)을 산출합니다.
   - 진행률 100% 도달 후 **`waiting_approval` (승인 ① 계획 승인 대기)** 상태로 안전하게 멈춥니다.
3. **계획 승인 ① (`Plan Approval`)**
   - 화면에 표시된 **계획 요약(3줄)**, **필수 점검 조건(Checks)** 및 **리스크 분석(Risks)**을 검토합니다.
   - 의견(Comment) 입력 후 **[계획 승인 ①]** 버튼 클릭!
4. **Terraform Provisioning 자동 실행**
   - 백그라운드에서 GCP VPC, Cloud SQL, Artifact Registry가 자동으로 빌드됩니다.
   - 완료 시 `TargetEnv` 접속 정보가 안전하게 전달됩니다.
5. **CDC DB Migration (초기 복제 + 실시간 변경분 동기화)**
   - Debezium / Kafka Connect를 통해 소스 RDS에서 대상 Cloud SQL로 테이블 데이터 및 WAL 변경분이 실시간 복제됩니다.
   - 행 수(Row count) 및 데이터 정합성 검증이 완료되면 **`waiting_approval` (승인 ② 전환 승인 대기)** 상태로 전환됩니다.
6. **전환 승인 ② (`Cutover Approval`) & App Deploy**
   - 트래픽 전환 시점을 결정하고 **[전환 승인 ②]** 버튼을 클릭합니다.
   - Cloudflare Worker가 트래픽을 일시 보류(HOLD) ➔ 최종 WAL 잔여분 반영 ➔ 목적지 Cloud Run으로 트래픽 스위칭 ➔ 보류 해제(RELEASE)가 순차적으로 완료되며 `succeeded` 상태로 최종 완료됩니다.

---

## 6. Phase 5: 비용 관리 및 실습 후 리소스 삭제 (Clean-up)

실습이 끝난 후 불필요한 클라우드 과금이 발생하지 않도록 자원을 즉시 정리합니다.

### 1) AWS 리소스 삭제 (터미널)
```bash
# 1. RDS 인스턴스 삭제 (최종 스냅샷 없이 즉시 삭제)
aws rds delete-db-instance \
  --db-instance-identifier target-db \
  --skip-final-snapshot \
  --delete-automated-backups \
  --region ap-northeast-2

# 2. RDS 파라미터 그룹 삭제
aws rds delete-db-parameter-group \
  --db-parameter-group-name pg16-cdc-group \
  --region ap-northeast-2

# 3. ECS 서비스 및 클러스터 삭제
aws ecs delete-service --cluster target-cluster --service target-app --force --region ap-northeast-2
aws ecs delete-cluster --cluster target-cluster --region ap-northeast-2

# 4. IAM 사용자 키 삭제
aws iam delete-access-key --user-name cloudmorph-migrator --access-key-id <YOUR_KEY_ID>
```

### 2) GCP 리소스 일괄 삭제 (프로젝트 단위)
```bash
# GCP는 프로젝트 전체를 삭제(Shut down)하면 연결된 모든 Cloud SQL, Cloud Run, VPC가 100% 영구 삭제되어 추가 과금이 완전히 차단됩니다.
gcloud projects delete $GCP_PROJECT_ID --quiet
```
