# 온프렘 목적지: OrbStack + Cloudflare

## 지원 범위

백엔드와 OrbStack Docker가 같은 맥북에서 실행되는 로컬 목적지 어댑터다. 로컬 Unix Docker 소켓을 명시적으로 사용한다. 원격 SSH 배포·다른 호스트 간 이미지 파일 전송은 이 구현에 포함하지 않는다. ECR은 외부 이미지의 출처이며 소스를 재빌드하지 않는다.

```
AWS RDS → AWS↔OCI VPN → OCI CDC
  → OCI cloudflared access tcp의 127.0.0.1:15432
  → Cloudflare Edge → OrbStack cloudflared → postgres:5432

사용자 → 기존 traffic-gate Worker → app.example.com
  → Cloudflare Edge → OrbStack cloudflared → was:8080 → postgres:5432
```

WAS와 PostgreSQL은 internal Docker 네트워크 `back`에만 연결하고 호스트 포트를 공개하지 않는다. cloudflared만 `back`과 외부 연결용 `front`에 함께 연결한다. DB 데이터는 프로젝트 전용 named volume에 보존한다. 기존 `infra/onprem_poc`의 실제 운영 컨테이너·볼륨을 자동 인수하지 않는다.

## 사전 준비와 입력

기본 도구: Python 3.11+, backend 의존성, Docker CLI/Compose 2.30 이상(raw env_file 지원), 실행 중인 OrbStack, openssl. 백엔드 실행 PATH에 Docker CLI가 있어야 한다.

`config.example.json`의 모든 경로·계정·호스트명·ID는 예시다. 다음 값을 준비한다.

| 입력 | 의미 |
|---|---|
| deployment_id, directory | 목적지별 고정 식별자·전용 절대 경로. 다른 목적지와 공유 금지 |
| docker_socket | 로컬 OrbStack Unix 소켓 |
| database | 목적지 DB 이름 |
| admin_ref | `secret://` 참조, 값 `{user, password}`. PG 초기 관리자이며 CREATE ROLE 가능 |
| app_ref | 다른 `secret://` 참조, 값 `{user, password}`. 앱 계정은 관리자가 아님 |
| cloudflare_ref | 값 `{api_token, tunnel_token}`. API 토큰과 connector 실행 토큰은 별개 |
| worker_secret_ref | 값 `{value}`. traffic-gate에 등록한 WORKER_SECRET과 동일해야 함 |
| account_id, zone_id, tunnel_id | 미리 만든 **이 배포 전용 remotely-managed tunnel**과 DNS 영역 |
| db_access_app_id | db_hostname에 이미 적용된 서비스 토큰 전용 Access 앱 |
| db_hostname, app_hostname | 서로 다른 공개 호스트명 |
| cdc_db_port | OCI의 cloudflared access tcp 로컬 포트. 기본 15432 |
| platform, allow_emulation | 원본 이미지 플랫폼과 에뮬레이션 명시 허용 |
| app_secret_refs | 추가 앱 Secret 변수 이름 → `{value}`를 저장한 secret:// 참조. 원본 ARN/GCP 이름은 자동 해석하지 않음 |

Tunnel 토큰·Access 서비스 토큰·Worker Secret은 서로 다르다. API 토큰에는 해당 tunnel 구성 읽기/쓰기, 해당 zone DNS 읽기/쓰기, Access 앱·정책 읽기 권한이 필요하다. 어댑터는 사용자·터널·Access 앱·정책·서비스 토큰을 새로 만들지 않는다.

OCI 노드에는 이미 같은 DB 호스트명과 서비스 토큰으로 인증하는 TCP 클라이언트가 있어야 한다. 이 어댑터는 OCI 배포/세션·Docker를 변경하지 않는다. provision의 probe는 `CDC_AGENT_URL`로 도달한 에이전트의 TCP 연결 검사이며 **노드 전체의 인증 검증은 아니다**. 실제 DB 관리자 인증·스키마/권한 확인은 기존 DB 마이그레이션 경로가 수행한다. 모의 에이전트의 probe는 실환경 provision 성공으로 인정하지 않는다.

기존 DB 모듈은 동일 원본 DB에 대해 다른 활성 마이그레이션 작업이 있으면 새 실행을 거절한다. 현재 실험용 작업이 남아 있다면 소유자와 상태·정리 범위를 확인하고 정리한 뒤 전체 워크플로를 실행해야 한다. 이 구현은 기존 작업을 자동 중단하지 않는다.

Worker 목적지 등록을 생략한 배포는 앱 검증까지 진행할 수 있지만, main의 온프렘 전환 단계는 worker_target_updated=true가 확인되어야 시작한다.

Worker에는 이 목적지 호스트를 허용하도록 TARGET_ALLOWED_SUFFIXES를 사전 설정하고 WORKER_SECRET을 맞춰 둔다. 기존 AWS/GCP 이전 작업이 이 Worker를 사용 중이면 목적지 등록이 간섭하므로 같은 Worker를 공유한 병행 전환은 금지한다.

## 운영 설정과 UI 선택

운영자가 `ONPREM_CONFIG_REF=secret://onprem/config`를 등록하면 요청에는 `target_infra: {"provider": "onprem"}`만 전달할 수 있다. 배포 경로·터널·계정은 이 참조에서 읽는다. 요청의 `extra_params.onprem_config_ref`가 있으면 운영 기본값보다 우선한다. 실제 환경값은 예시에 고정하지 않는다.

## Secret 등록과 실행 요청

기존 `app.core.secrets.put_secret` 또는 로컬 비밀 등록 도구로 값들을 먼저 등록한다. 로그·명령 인자에 원문을 넣지 않는다. 설정 자체는 `put_secret("onprem/config", {"json": <설정 JSON 문자열>})` 형태로 저장한다.

워크플로 요청(API):

```json
{
  "service_name": "target-app",
  "aws_region": "ap-northeast-2",
  "aws_credentials_ref": "secret://default/source-cloud",
  "db_admin_ref": "secret://default/source-db-admin",
  "target_infra": {
    "provider": "onprem",
    "extra_params": {"onprem_config_ref": "secret://onprem/config"}
  }
}
```

현재 추가 경로는 API 입력이다. 기존 프런트엔드 목적지 폼에 온프렘 전용 설정 UI는 추가하지 않는다. 원본 assessment의 현재 Cloud Run 호환성·계획 판정은 유지하므로 AWS 특화 기능이 있는 임의 앱까지 지원 범위를 넓힌 것은 아니다.

## 고정 in/out

기존 `ProvisioningOutput`, `AppDeployOutput`를 상속한 출력 모델을 반환한다. target은 기존 `db_migration.schemas.TargetInput`과 같은 필드 집합이며 중첩 필드까지 추가 필드를 금지한다. 생성된 JSON Schema를 함께 제공한다.

- `provision`: `{target, problems, changes, applied}`.
- `deploy_app`: `{app_url, healthy, target, changes, applied, worker_target_updated}`.
- `target`: `{cloud, project_id, region, db, network, app, credentials_ref}`.
- `cloud=onprem`, `project_id=null`, `region=local`.
- `target.db`: `{instance, private_ip, port, database, version, ssl_mode}`. private_ip/port는 **OCI 기준** `127.0.0.1:15432`다.
- `network`: 기존 키를 유지하며 GCP 전용 값은 null. 가짜 VPC·CIDR을 만들지 않는다.
- `app`: `{url, db_user}`. provision 때 url=null, deploy_app 이후 https 앱 URL.
- `credentials_ref`: 기존 cloud/admin/ddl_user/sink_user. cloud에는 설정 참조, admin에는 실제 관리자 참조. `pass_onprem=true`에서는 cloud를 GCP 키로 해석하지 않는다.
- DB 복제 runner는 target을 재작성하지 않고 `options.pass_onprem=true`만 추가한다. 시뮬레이션 분기의 skip_vpn도 이 옵션을 덮어쓰지 않는다.
- deploy_app의 target은 provision의 target에서 app.url만 바뀐다. 이미지·Compose·Docker 소켓·터널 토큰 등은 target에 넣지 않는다.

`directory/state.json`에 설정 fingerprint, 고정 target, 성공 이미지 버전/아카이브 체크섬을 저장한다. 원문 비밀은 저장하지 않는다. DB·앱·Tunnel Secret은 별도 `secrets/` 아래 0600 파일에 저장한다. directory와 state는 0700/0600 권한 및 프로세스 간 잠금으로 보호한다. 설정이 바뀐 기존 DB를 묵시적으로 재초기화·교체하지 않는다.

## provision 순서

1. 설정·Secret 참조·DB 사용자 분리 검증, 배포 잠금.
2. DB Access 앱 domain과 Service Auth(API decision=non_identity) 정책 확인. 자동 활성화하지 않음.
3. SSL 인증서·PG 접근 규칙·Compose·Secret 파일 준비. 새 PostgreSQL·cloudflared 실행/기존 배포 재사용. TCP는 SSL로 제한.
4. 로컬 PostgreSQL 관리자 비밀번호·SSL 인증 확인, 앱 역할 생성.
5. DB ingress/DNS 구성. 기존 관련 없는 ingress와 옵션을 보존하고 충돌한 DNS/호스트를 덮어쓰지 않음.
6. 터널 healthy와 OCI 에이전트 TCP probe 확인 후에만 성공 target 반환.
7. 기존 DB 모듈이 AWS↔OCI VPN과 초기 복사·CDC를 수행. 기존 공유 VPN/state에 새 작업을 임의 적용하면 안 됨.

## deploy_app 순서

1. provision 상태 및 동일 target 확인. Worker는 SOURCE 상태여야 함.
2. 원본 ECR 주소는 digest 필수. 기존 aws_ecr_login/load_aws_credentials 재사용.
3. 임시 인증 설정으로 pull → docker save → 파일 SHA-256 → docker load → image ID/플랫폼 비교. 인증 파일 삭제.
4. 목적지 DB·Worker·추가 앱 Secret 주입. WAS만 up --no-deps로 실행.
5. WAS 내부 DB health, 비인증 업무 요청 차단, 인증 업무 요청 성공 확인.
6. 앱 ingress/DNS 연결, 외부 HTTPS health·업무 API·비인증 차단 확인.
7. 성공 이미지 상태 저장 후 Worker의 목적지 후보 URL만 등록. switch/hold/release는 호출하지 않음.
8. 실패하면 이전 WAS 이미지+환경+Compose 복원. 최초 배포 실패는 WAS만 중지. DB 볼륨은 유지. Cloudflare 추가 경로는 삭제하지 않으며 실패 시 중지된 WAS로 502가 날 수 있음.

현재 지원하는 앱은 기존 Python samples/target-app의 8080 포트·/health/ready·/api/work-items·Worker Secret 계약이다. 실제 원본 ECR 이미지가 Worker 인증을 지원하지 않으면 배포 검증이 실패하며 공개 앱 경로는 처음 추가하기 전에 차단된다. 환경 변수만 주입했다고 보호된 앱으로 판정하지 않는다.

## 검증과 제한

단위/계약 테스트는 Docker/Cloudflare 실행기를 주입해 실서비스를 변경하지 않는다. 기존 DB 입력으로 역직렬화, target 동등성, runner pass_onprem 전달, Secret 비노출, DNS 충돌, 실패 복구, image ID 검증을 포함한다.

현재 구현에는 원격 SSH 호스트 간 전송, 기존 PoC 리소스 자동 인수, Cloudflare 계정/터널/Access 생성, 기존 동기화 데이터가 있는 DB에 신규 작업을 덮어쓰는 기능, 자원 삭제 API가 없다. 기존 finish가 정리하는 것은 복제/VPN 자원이며 온프렘 데이터 볼륨은 자동 삭제하지 않는다. 자체 서명 SSL은 require이며 verify-full 검증은 제공하지 않는다. 전체 원본을 바꾸는 스키마 변경·cutover·실환경 배포는 이 브랜치 구현 과정에서 수행하지 않는다.

공식 참고: [Cloudflare Tunnel API 구성](https://developers.cloudflare.com/tunnel/get-started/), [cloudflared 실행 옵션](https://developers.cloudflare.com/tunnel/reference/run-parameters/), [Compose 변수 처리](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/).
