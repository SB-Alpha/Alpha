# 온프렘 시험 입력 위치와 초기화 범위

## 입력은 target이 아니라 요청/설정에서 시작

`target`은 사용자가 만드는 파일이 아니라 provision()이 준비·검증 후 반환하는 결과다.

1. `config.input.json`의 빈 값을 채운다.
   - 목적지 전용 빈 deployment_id/directory, OrbStack Docker socket, 새 DB 이름.
   - 관리자/앱/Cloudflare/Worker Secret의 참조.
   - Cloudflare account/zone/tunnel/DB Access 앱 ID, 앱/DB 호스트명.
   - Worker 목적지 갱신은 초기 시험에서 false. 실제 컷오버 시험 전에 별도 Worker와 함께 설정한다.
2. 각 Secret을 백엔드의 기존 app.core.secrets 저장소에 등록한다. 원문은 Git/일반 요청에 넣지 않는다.
   - admin_ref/app_ref: {user,password}
   - cloudflare_ref: {api_token,tunnel_token}
   - worker_secret_ref: {value}
3. 설정 JSON 문자열을 `{json: <설정 문자열>}`로 `secret://onprem-test/config`에 등록한다.
4. `workflow-request.input.json`에 원본 서비스, AWS 접근 Secret 참조, 원본 DB 관리자 Secret 참조를 입력한다.
5. 이 브랜치의 백엔드에서 POST /runs로 요청한다. assessment 후 승인 ①에서 멈춘다.
6. 승인 ① 후 provision이 target을 생성하고 DB 복제·앱 배포로 이어진다. 초기 시험은 직접 service.provision()만 호출해 고정 출력부터 검증할 수도 있다.

## 기존 환경과 분리

새 어댑터는 기존 PoC의 Compose·DB·볼륨을 자동 인수하지 않는다. 새 빈 디렉터리·고유 deployment_id·Compose 프로젝트·전용 터널/호스트명을 준비하고, 기존 데이터와 공유 VPN/Terraform state를 재사용하거나 초기화하지 않는다.

입력 파일에는 실제 호스트명·계정 ID·경로를 고정하지 않았다. 시험 실행 시 입력하며 비밀번호와 토큰은 Secret 참조로 전달한다. 다른 활성 작업과 동일 원본을 쓰는 새 워크플로는 기존 사전 검사에서 거절될 수 있다.

## 정리와 별개인 사전 조건

- 이 어댑터는 DB용 Cloudflare Access 앱과 Service Auth 정책이 이미 준비된 것을 요구한다. OCI의 TCP 클라이언트 서비스 토큰 준비 후 진행해야 한다.
- Worker 인증을 구현하지 않은 ECR 이미지는 deploy_app의 인증 검사에서 거절된다. 인증 코드가 포함된 호환 이미지가 필요하다.
- 처음에는 update_worker_target=false로 검증한다. 실제 트래픽 전환은 다른 이전 작업과 분리된 전용 Worker와 실행 시 입력한 주소를 사용한다. 시험 후 목적지 등록과 임시 자원을 정리한다.

## 확인한 범위와 남은 검증

- 단위·계약 테스트는 Docker/Cloudflare/CDC 실행기를 주입하며 실서비스를 변경하지 않는다.
- 별도 로컬 실험에서 PostgreSQL SSL 인증과 앱 계정, ECR pull → save → 체크섬 → load, WAS의 시험 데이터 조회를 확인했다. 별도 호스트 간 파일 전송은 시험하지 않았다.
- 인증 코드가 없는 이미지가 검증에서 거절되는 것을 확인했다.
- 새 Cloudflare 터널·OCI 클라이언트를 연결한 전체 provision/DB 복제/앱 배포/전환은 미검증이다.
- 기존 팀 Worker의 SOURCE 경유 앱·DB 읽기는 확인했지만, 이 온프렘 목적지로의 Worker 전환 시험은 수행하지 않았다.
- Cloudflare Load Balancer pool 등록과 가중 분산은 이 PR의 구현 범위에 포함되지 않는다.
