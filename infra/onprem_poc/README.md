# 온프렘 목적지 PoC

CDC 목적지를 "온프렘"으로 흉내 내는 한 호스트 구성입니다. 클라우드 목적지(Cloud SQL 등) 대신 이 PostgreSQL 로 복제하고 전환하는 흐름을 시험합니다. 운영용이 아닙니다.

- `postgres`: PostgreSQL 16 (`wal_level=logical`, `ssl=on` 자체 서명). `back` 네트워크(internal)에만 있어 밖에서 직접 붙을 수 없습니다.
- `cloudflared`: Cloudflare Tunnel. DB와 외부(`front`)를 잇는 유일한 컨테이너입니다. CDC 노드는 사이드카의 `cloudflared access tcp` 로 이 터널을 지나 DB에 붙습니다.
- `compose.was.yml`: 이후 단계용 WAS 컨테이너(선택). 가져온 이미지와 `.was.env` 가 필요합니다.

## 실행
1. `.env.example` 을 `.env` 로 복사하고 `PG_PASSWORD`, `TUNNEL_TOKEN`(Cloudflare 터널 토큰)을 채웁니다.
2. `certs/server.crt`, `certs/server.key` 를 만듭니다(자체 서명, 예: `openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=postgres" -keyout certs/server.key -out certs/server.crt`).
3. `docker compose up -d`

`.env`, `certs/`, `.was.env` 는 git 에 올리지 않습니다. 터널 호스트 이름과 라우트(`tcp://postgres:5432`)는 Cloudflare 대시보드에서 설정하며 코드에 넣지 않습니다.

## 알려진 한계
- 접근 보호(Cloudflare Access)는 따로 설정해야 합니다. 설정 전에는 호스트 이름과 DB 비밀번호만으로 접속됩니다.
- `cloudflared:latest` 를 쓰므로 고정이 필요하면 태그를 지정하세요.
