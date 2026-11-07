# 목적지 클라우드 어댑터 명세

GCP 외의 목적지(Azure, NCP)를 붙일 때 맞춰야 하는 계약입니다. 어댑터와 자기 Terraform 은 workflows 의 계약(`schemas.py`)을 고치지 않고 만들 수 있습니다. 다만 CDC 경로와 계획 모듈은 아직 GCP 기준이라 담당자 협의가 필요합니다. 아래 "어댑터만으로 끝나지 않는 부분"을 먼저 읽어 주세요.

현재 등록된 어댑터는 `gcp` 뿐입니다. 등록되지 않은 목적지는 `POST /runs` 가 `422` 로 거절합니다. 다른 클라우드를 대신 구축하지 않기 위해서입니다.

## 전체 흐름

```
frontend 폼 ─ MigrationRequest.target_infra ─▶ workflows
assessment  ─ plan.target_infra ─▶ adapter.provision ─ {target, ...} ─▶ db_migration
                                   adapter.deploy_app ─ {app_url, healthy, ...} ─▶ 승인 ② ─▶ cutover
```

## 1. 요청 입력: `MigrationRequest.target_infra`

`apps/backend/app/features/workflows/schemas.py` 의 `TargetInfraConfig` 입니다.

```json
{
  "service_name": "target-app",
  "aws_region": "ap-northeast-2",
  "aws_credentials_ref": "secret://aws/default",
  "db_admin_ref": "secret://db/admin",
  "target_region": "koreacentral",
  "target_infra": {
    "provider": "azure",
    "region": "koreacentral",
    "project_id": "<구독 ID 또는 NCP 프로젝트>",
    "extra_params": { "resource_group": "alpha-rg", "client_secret": "secret://azure/client" }
  }
}
```

| 필드 | 설명 |
| --- | --- |
| `provider` | `gcp`, `azure`, `ncp` 중 하나. `target_infra` 를 생략하면 `gcp` |
| `region` | 목적지 리전. `target_region` 을 비우면 이 값이 들어가고, 둘이 다르면 `422` |
| `project_id` | 클라우드별 소속 단위. GCP 는 프로젝트 ID, 비우면 `GCP_PROJECT_ID` |
| `extra_params` | 어댑터 전용 값. 이름에 `password`, `secret`, `token`, `api_key`, `private_key`, `credential` 이 들어가면 `secret://` 참조만 받습니다 |

## 2. 백엔드 어댑터

`apps/backend/app/features/workflows/target_adapters.py` 의 `TargetAdapter` 를 구현하고 `TARGET_ADAPTERS` 에 등록합니다. runner 는 고치지 않습니다.

```python
class AzureTargetAdapter:
    provider = "azure"

    def provision(self, run_id, target_infra, config, ctx): ...
    def deploy_app(self, run_id, target_infra, config, ctx): ...

TARGET_ADAPTERS = {"gcp": GcpTargetAdapter(), "azure": AzureTargetAdapter()}
```

| 인자 | 내용 |
| --- | --- |
| `run_id` | run ID. 비밀 이름표에 씁니다 (`secret://<run_id>/...`) |
| `target_infra` | 승인 ① 을 받은 `plan.target_infra` (3절) |
| `config` | 요청의 `TargetInfraConfig`. 없을 수 있습니다 |
| `ctx` | 진행 보고 통로. `ctx.report("running", 0~100, metrics, message)` |

끝은 값을 돌려주거나 예외를 내는 것으로 정해집니다. 예외를 내면 그 단계가 실패로 기록됩니다.

### `provision` 출력

`db_migration` 이 `target` 을 그대로 받습니다. 키를 바꾸면 안 됩니다. 기준은 `provisioning/target.py` 의 `build_target` 입니다.

```json
{
  "target": {
    "project_id": "...",
    "region": "...",
    "network": { "vpc_name": "...", "subnet_cidrs": ["10.60.0.0/24"], "psa_range_cidr": "10.61.0.0/24" },
    "db": { "instance": "...", "private_ip": "10.61.0.5", "port": 5432, "database": "app", "version": "16", "ssl_mode": "ENCRYPTED_ONLY" },
    "app": { "url": null, "db_user": "app" },
    "credentials_ref": { "cloud": "secret://<run_id>/target-cloud", "admin": "secret://<run_id>/target-admin" }
  },
  "problems": [],
  "changes": { "add": 0, "change": 0, "destroy": 0 },
  "applied": true
}
```

`problems` 는 CDC 가 목적지에 닿지 못하게 만드는 문제입니다. `target.find_problems` 기준은 다음과 같습니다.

- `db.ssl_mode` 가 `ENCRYPTED_ONLY`
- PostgreSQL 16 이상
- `subnet_cidrs`, `psa_range_cidr` 가 OCI `10.50.0.0/16`, AWS `172.31.0.0/16` 와 겹치지 않음

`db.private_ip` 가 사설 IP 가 아니면 화면 지표 `cloud_sql_private_ip` 가 `PUBLIC` 으로 나옵니다.

### `deploy_app` 출력

```json
{
  "app_url": "https://...",
  "healthy": true,
  "target": { "...": "provision 과 같은 모양" },
  "changes": { "add": 0, "change": 0, "destroy": 0 },
  "applied": true,
  "worker_target_updated": true
}
```

- `healthy` 는 `app_url + /health/ready` 가 200 인지입니다. 승인 조건이 이 값을 봅니다.
- `WORKER_URL` 이 있으면 앱이 준비된 뒤 Worker 의 목적지 주소를 `app_url` 로 바꿉니다 (`provisioning/worker_client.py`).
- 실시간 점검은 `https` 주소만 호출합니다.

## 3. 계획 입력: `plan.target_infra`

`assessment/schemas.py` 의 `TargetInfra` 입니다. 필드 이름이 곧 Terraform 변수 이름입니다.

| 변수 | 설명 |
| --- | --- |
| `region`, `service_name` | 목적지 리전, 서비스 이름 |
| `source_image` | 원본 ECR 주소. 목적지 레지스트리로 복사는 어댑터가 합니다 |
| `container_port`, `cpu`, `memory`, `health_check_path` | 원본에서 읽은 앱 사양. `cpu` 는 vCPU, `memory` 는 MiB |
| `env_vars` | 비밀이 아닌 환경 변수 |
| `secret_refs` | 환경 변수 이름 → 비밀 이름 |
| `db_version`, `db_name`, `db_user`, `db_tier`, `db_disk_size_gb` | DB 사양. `db_tier` 는 지금 Cloud SQL 등급입니다 |
| `db_logical_decoding` | CDC 대상이라 항상 켭니다 |

## 4. Terraform 출력

`infra/terraform/<provider>/` 에 둡니다. 다른 Terraform 과 상태를 공유하지 않습니다. `*.tfstate`, `*.tfvars` 는 커밋하지 않고 예시는 `*.tfvars.example` 로 둡니다.

GCP 어댑터가 `target` 을 만들 때 꼭 필요한 출력은 다음과 같습니다 (`target.REQUIRED_OUTPUTS`). 다른 클라우드도 같은 이름으로 내보내면 `build_target` 을 다시 쓸 수 있습니다.

| 출력 | `target` 위치 |
| --- | --- |
| `vpc_name`, `subnet_cidrs`, `psa_range_cidr` | `network.*` |
| `db_instance`, `db_private_ip`, `db_port`, `db_name`, `db_version`, `db_ssl_mode` | `db.*` |
| `db_user`, `app_url` (`app_url` 은 앱 배포 전 `null`) | `app.*` |

## 5. 환경 변수

| 이름 | 쓰는 곳 |
| --- | --- |
| `GCP_PROJECT_ID` | GCP 어댑터. 요청의 `project_id` 가 없을 때 |
| `PROVISIONING_TF_DIR`, `TERRAFORM_BIN` | GCP 어댑터의 Terraform 경로, 실행 파일 |
| `WORKER_URL`, `WORKER_TOKEN_FILE` | 공통. 앱 준비 후 Worker 목적지 변경 |

새 어댑터의 변수는 `apps/backend/.env.example` 에 이름만 추가합니다. 값이나 비밀 원문은 넣지 않습니다.

## 6. 프런트엔드

`apps/frontend/lib/adapters/types.ts` 의 어댑터에서 `isAvailable` 을 `true` 로 바꿉니다. 그전까지 폼에는 "어댑터 준비 중" 으로 비활성화됩니다. 표시 이름과 기본 리전도 이 파일에 있습니다.

## 7. 어댑터만으로 끝나지 않는 부분

아래는 지금 GCP 기준으로 고정돼 있어, 실제 Azure 전환까지 가려면 담당자와 함께 바꿔야 합니다.

| 위치 | 고정된 내용 | 담당 |
| --- | --- | --- |
| `db_migration/vpn.py`, `infra/db_migration/terraform/gcp` | OCI↔GCP VPN. 목적지 네트워크 입력이 GCP 값입니다 (`vpc_name`, `psa_range_cidr`, `cloud_ref`) | 안재현 |
| `assessment/rules.py` | 목적지 PostgreSQL 버전, Cloud SQL 등급 선택, GCP 자원 이름 규칙 | 김준석, 이주호 |
| `provisioning/target.py` | `find_problems` 의 `ssl_mode` 값과 버전 표기 (`POSTGRES_16`) | 박준서 |
| 화면 | `layout.tsx` 머리글 문구, 대시보드의 `Cloud SQL 사설망` 칸 이름 | 박재현 |

원본은 지금 AWS 만 지원합니다. 이 명세는 목적지만 다룹니다. DB 엔진은 PostgreSQL 기준이며, 다른 엔진 지원 여부는 팀 합의가 필요합니다.

## 8. 시험

`apps/backend/tests/workflows/test_target_adapters.py` 의 `test_registering_an_adapter_routes_provisioning_without_runner_changes` 가 어댑터 등록 방식의 예시입니다. 실제 클라우드 없이 가짜 어댑터로 runner 연결을 확인합니다.

```bash
cd apps/backend
pytest tests/workflows/test_target_adapters.py
```
