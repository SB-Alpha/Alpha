"""로컬 OrbStack Docker 실행기. WAS/DB는 internal 네트워크에만 두고 포트를 게시하지 않는다."""

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from app.features.provisioning.image_transfer import (
    aws_ecr_login,
    load_aws_credentials,
    parse_image,
)
from app.features.provisioning.terraform import ProvisioningError
from .state import atomic_write


def literal(value):
    return "'" + value.replace("'", "''") + "'"


class DockerRuntime:
    def __init__(self, config, runner=subprocess.run):
        self.c = config
        self.runner = runner
        self.root = config.directory
        self.project = "alpha-onprem-" + config.deployment_id
        self.compose_path = self.root / "compose.json"
        self.secrets = self.root / "secrets"

    def command(self, args, *, stdin=None, env=None, timeout=900):
        child_env = {
            k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ
        }
        child_env.update(env or {})
        try:
            result = self.runner(
                args,
                input=stdin,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            raise ProvisioningError(
                "온프렘 명령 실행 실패 (출력은 비밀값 보호를 위해 제외)"
            ) from None
        if result.returncode:
            raise ProvisioningError(
                "온프렘 명령이 실패했습니다 (출력은 비밀값 보호를 위해 제외)"
            )
        return result.stdout

    def docker(self, args, **kwargs):
        return self.command(
            ["docker", "--host", "unix://" + str(self.c.docker_socket), *args], **kwargs
        )

    def compose(self, args):
        return self.docker(
            [
                "compose",
                "--project-name",
                self.project,
                "--file",
                str(self.compose_path),
                *args,
            ]
        )

    def check_host(self):
        info = json.loads(self.docker(["info", "--format", "{{json .}}"], timeout=30))
        if info.get("OSType") != "linux":
            raise ProvisioningError("Linux Docker 엔진이 필요합니다")
        import re

        version = self.docker(["compose", "version", "--short"], timeout=15).strip()
        match = re.match(r"v?(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (2, 30):
            raise ProvisioningError(
                "Secret raw env_file 처리를 위해 Compose 2.30 이상이 필요합니다"
            )
        return info

    def secret_file(self, name, value):
        self.secrets.mkdir(parents=True, exist_ok=True, mode=0o700)
        p = self.secrets / name
        if p.is_symlink() or (p.exists() and p.read_text() != value):
            raise ProvisioningError(
                "기존 배포 Secret 변경을 감지했습니다. 자동 교체하지 않습니다"
            )
        atomic_write(p, value)

    def compose_document(self, admin, image=None, app_env=None):
        c = self.c
        postgres = {
            "image": c.postgres_image,
            "restart": "unless-stopped",
            "networks": ["back"],
            "environment": {
                "POSTGRES_USER": admin["user"],
                "POSTGRES_DB": c.database,
                "POSTGRES_PASSWORD_FILE": "/run/secrets/db_admin",
            },
            "secrets": ["db_admin"],
            "volumes": [
                "pgdata:/var/lib/postgresql/data",
                str(self.root / "certs") + ":/certs:ro",
                str(self.root / "pg_hba.conf") + ":/etc/alpha/pg_hba.conf:ro",
            ],
            "entrypoint": [
                "sh",
                "-c",
                'cp /certs/server.* /tmp/ && chown postgres:postgres /tmp/server.* && chmod 600 /tmp/server.key && exec docker-entrypoint.sh "$@"',
                "--",
            ],
            "command": [
                "postgres",
                "-c",
                "ssl=on",
                "-c",
                "ssl_cert_file=/tmp/server.crt",
                "-c",
                "ssl_key_file=/tmp/server.key",
                "-c",
                "hba_file=/etc/alpha/pg_hba.conf",
            ],
        }
        tunnel = {
            "image": c.cloudflared_image,
            "restart": "unless-stopped",
            "networks": ["back", "front"],
            "command": ["tunnel", "--no-autoupdate", "run"],
            "env_file": [{"path": str(self.secrets / "tunnel.env"), "format": "raw"}],
        }
        services = {"postgres": postgres, "cloudflared": tunnel}
        if image:
            services["was"] = {
                "image": image,
                "platform": c.platform,
                "restart": "unless-stopped",
                "networks": ["back"],
                "env_file": [{"path": str(self.secrets / "was.env"), "format": "raw"}],
                "mem_limit": "512m",
                "cpus": 1.0,
            }
        return {
            "services": services,
            "volumes": {"pgdata": {}},
            "networks": {"back": {"internal": True}, "front": {}},
            "secrets": {"db_admin": {"file": str(self.secrets / "db_admin")}},
        }

    def provision(self, admin, app, tunnel_token):
        self.check_host()
        self.secret_file("db_admin", admin["password"])
        self.secret_file("tunnel.env", "TUNNEL_TOKEN=" + tunnel_token + "\n")
        certs = self.root / "certs"
        certs.mkdir(exist_ok=True, mode=0o700)
        if not (certs / "server.key").exists():
            self.command(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "365",
                    "-subj",
                    "/CN=postgres",
                    "-keyout",
                    str(certs / "server.key"),
                    "-out",
                    str(certs / "server.crt"),
                ],
                timeout=30,
            )
            (certs / "server.key").chmod(0o600)
        atomic_write(
            self.root / "pg_hba.conf",
            "local all all trust\nhostssl all all all scram-sha-256\nhostnossl all all all reject\n",
        )
        (self.root / "pg_hba.conf").chmod(0o644)
        # 기존 WAS는 유지하고 up --no-deps로 지정 서비스만 갱신한다.
        if not self.compose_path.exists():
            atomic_write(
                self.compose_path, json.dumps(self.compose_document(admin), indent=2)
            )
        self.compose(["up", "-d", "--no-deps", "postgres", "cloudflared"])
        self.wait_db(admin)
        sql = (
            "\\set ON_ERROR_STOP on\n"
            "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', "
            + literal(app["user"])
            + ", "
            + literal(app["password"])
            + ") "
            "WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname="
            + literal(app["user"])
            + ");\\gexec\n"
            f'GRANT CONNECT ON DATABASE "{self.c.database}" TO "{app["user"]}";\n'
            f'GRANT USAGE ON SCHEMA public TO "{app["user"]}";\n'
        )
        sql = sql.replace(");\\gexec", ")\\gexec")
        self.compose(["ps"])  # 진단 출력은 외부로 반환하지 않는다.
        self.docker(
            [
                "compose",
                "--project-name",
                self.project,
                "--file",
                str(self.compose_path),
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                admin["user"],
                "-d",
                self.c.database,
            ],
            stdin=sql,
        )

    def wait_db(self, admin):
        deadline = time.monotonic() + self.c.health_timeout_s
        while True:
            try:
                # TCP+SSL로 실제 관리자 비밀번호도 검증한다.
                version = self.docker(
                    [
                        "compose",
                        "--project-name",
                        self.project,
                        "--file",
                        str(self.compose_path),
                        "exec",
                        "-T",
                        "postgres",
                        "sh",
                        "-c",
                        'export PGPASSWORD="$(cat /run/secrets/db_admin)"; exec psql "host=127.0.0.1 sslmode=require" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SHOW server_version_num"',
                    ],
                    timeout=15,
                )
                if not 160000 <= int(version.strip()) < 170000:
                    raise ProvisioningError(
                        "온프렘 목적지는 PostgreSQL 16이어야 합니다"
                    )
                return
            except ProvisioningError:
                if time.monotonic() >= deadline:
                    raise ProvisioningError(
                        "온프렘 PostgreSQL 인증·SSL 준비 확인 시간 초과"
                    ) from None
                time.sleep(2)

    def probe_cdc(self):
        from app.features.db_migration.client import AgentSettings, CdcAgentClient

        settings = AgentSettings.from_env()
        if settings.is_simulation:
            raise ProvisioningError(
                "실제 온프렘 준비 확인에 모의 CDC 에이전트를 사용할 수 없습니다"
            )
        result = CdcAgentClient(settings).probe(
            [
                {"host": self.c.cdc_db_host, "port": self.c.cdc_db_port},
            ]
        )
        # 기존 probe 계약: 결과 배열의 각 항목에 ok가 들어 있다.
        if not result or not all(row.get("ok") for row in result):
            raise ProvisioningError("OCI에서 온프렘 DB 터널에 접속할 수 없습니다")

    def image(self, source_image, credentials_ref):
        source = parse_image(source_image)
        if not source.digest:
            raise ProvisioningError("온프렘 이미지 배포에는 원본 digest가 필요합니다")
        credentials = load_aws_credentials(credentials_ref)
        login = aws_ecr_login(source.registry, credentials)
        releases = self.root / "images"
        releases.mkdir(exist_ok=True, mode=0o700)
        tag = (
            "alpha-onprem/"
            + self.c.deployment_id
            + ":"
            + source.digest.split(":")[1][:24]
        )
        archive = releases / (source.digest.split(":")[1] + ".tar")
        with tempfile.TemporaryDirectory(prefix="alpha-onprem-auth-") as folder:
            config = Path(folder) / "config.json"
            auth = base64.b64encode(
                (login.username + ":" + login.password).encode()
            ).decode()
            atomic_write(
                config, json.dumps({"auths": {login.registry: {"auth": auth}}})
            )
            env = {"DOCKER_CONFIG": folder}
            self.docker(
                ["pull", "--platform", self.c.platform, source.pull_ref], env=env
            )
            info = json.loads(
                self.docker(["image", "inspect", source.pull_ref], env=env)
            )[0]
            if source.pull_ref not in info.get("RepoDigests", []):
                raise ProvisioningError("다운로드 이미지 digest가 원본과 다릅니다")
            if info["Os"] + "/" + info["Architecture"] != self.c.platform:
                raise ProvisioningError("원본 이미지와 요청 플랫폼이 다릅니다")
            host = self.check_host().get("Architecture")
            native = {"aarch64": "arm64", "x86_64": "amd64"}.get(host, host)
            if self.c.platform.split("/")[1] != native and not self.c.allow_emulation:
                raise ProvisioningError(
                    "이미지 아키텍처가 다릅니다. 에뮬레이션 허용을 명시하세요"
                )
            self.docker(["tag", source.pull_ref, tag], env=env)
            temporary = archive.with_suffix(".partial")
            try:
                self.docker(["save", "--output", str(temporary), tag], env=env)
                with temporary.open("rb") as f:
                    digest = hashlib.file_digest(f, "sha256").hexdigest()
                os.replace(temporary, archive)
            finally:
                temporary.unlink(missing_ok=True)
            atomic_write(archive.with_suffix(".sha256"), digest + "\n")
            with archive.open("rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != digest:
                    raise ProvisioningError("이미지 아카이브 체크섬 불일치")
            self.docker(["load", "--input", str(archive)], env=env)
            loaded = json.loads(self.docker(["image", "inspect", tag], env=env))[0]
            if loaded["Id"] != info["Id"]:
                raise ProvisioningError("로드한 이미지 ID가 원본과 다릅니다")
        return {
            "image": tag,
            "source": source.pull_ref,
            "image_id": info["Id"],
            "archive_sha256": digest,
        }

    def deploy(self, admin, env, image):
        # WAS Secret은 DB Secret과 분리하며 호출자가 복구용 이전 파일을 보존한다.
        atomic_write(
            self.secrets / "was.env",
            "".join(k + "=" + v + "\n" for k, v in env.items()),
        )
        atomic_write(
            self.compose_path, json.dumps(self.compose_document(admin, image), indent=2)
        )
        self.compose(["up", "-d", "--no-deps", "was"])

    def internal_health(self, worker_secret):
        # 기존 samples/target-app의 Python 이미지 계약을 사용한다.
        code = """import urllib.request,urllib.error,json,sys
s=json.load(sys.stdin)['secret']
b='http://127.0.0.1:8080'
assert urllib.request.urlopen(b+'/health/ready',timeout=5).status==200
try:
 urllib.request.urlopen(b+'/api/work-items?limit=1',timeout=5)
 raise RuntimeError('worker guard missing')
except urllib.error.HTTPError as e:
 assert e.code in (401,403)
r=urllib.request.Request(b+'/api/work-items?limit=1',headers={'X-Alpha-Worker-Secret':s})
assert urllib.request.urlopen(r,timeout=5).status==200
"""
        # Secret은 표준 입력으로만 전달하며 argv·로그에 넣지 않는다.
        script = (
            "import io,sys; p="
            + repr(code)
            + "; d=sys.stdin.read(); sys.stdin=io.StringIO(d); exec(p)"
        )
        deadline = time.monotonic() + self.c.health_timeout_s
        while True:
            try:
                self.docker(
                    [
                        "compose",
                        "--project-name",
                        self.project,
                        "--file",
                        str(self.compose_path),
                        "exec",
                        "-T",
                        "was",
                        "python",
                        "-c",
                        script,
                    ],
                    stdin=json.dumps({"secret": worker_secret}),
                    timeout=15,
                )
                return
            except ProvisioningError:
                if time.monotonic() >= deadline:
                    raise ProvisioningError(
                        "WAS DB 연결·Worker 인증 검사 실패 (인증 미지원 이미지 포함)"
                    ) from None
                time.sleep(2)

    def external_health(self, secret):
        headers = {"X-Alpha-Worker-Secret": secret, "User-Agent": "alpha-onprem/1.0"}
        deadline = time.monotonic() + self.c.health_timeout_s
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            while True:
                try:
                    health = client.get(
                        "https://" + self.c.app_hostname + "/health/ready",
                        headers=headers,
                    )
                    api = client.get(
                        "https://" + self.c.app_hostname + "/api/work-items?limit=1",
                        headers=headers,
                    )
                    denied = client.get(
                        "https://" + self.c.app_hostname + "/api/work-items?limit=1"
                    )
                    if (
                        health.status_code == api.status_code == 200
                        and denied.status_code in (401, 403)
                    ):
                        return
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    raise ProvisioningError(
                        "Cloudflare를 통한 WAS 준비·인증 확인 시간 초과"
                    )
                time.sleep(2)

    def stop_was(self):
        # compose 복구 파일에 was가 없더라도 이 배포의 WAS만 중지할 수 있어야 한다.
        ids = self.docker(
            [
                "ps",
                "-aq",
                "--filter",
                "label=com.docker.compose.project=" + self.project,
                "--filter",
                "label=com.docker.compose.service=was",
            ]
        ).split()
        if ids:
            self.docker(["stop", "--time", "10", *ids])
