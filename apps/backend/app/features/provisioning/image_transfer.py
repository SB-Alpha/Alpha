# 원본 이미지를 목적지 저장소로 복사한다. app_deploy 에서 앱을 만들기 전에 이미지가 목적지 저장소에 있어야 한다.
#
# 순서: 원본 주소 해석 -> 두 저장소의 로그인 토큰 받기 -> crane 으로 복사 -> digest 확인 -> digest 로 고정한 주소 돌려주기.
# 토큰은 환경에서 받아 오는 함수(aws_ecr_login, gcp_login)로 분리해 두었다. 로컬에서는 내 로그인,
# 서버에서는 서버에 붙은 신분을 쓰므로 복사 코드는 어디서 돌든 같다.
# 토큰과 키는 오류 메시지·로그에 넣지 않는다.
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .terraform import ProvisioningError

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# <12자리 계정>.dkr.ecr.<리전>.amazonaws.com (중국 리전 .com.cn, FIPS 포함)
_ECR_HOST = re.compile(r"^(?P<account>\d{12})\.dkr\.ecr(?:-fips)?\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$")

GCP_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
COPY_TIMEOUT_S = 900.0

# 저장소 주소가 없는 이름(nginx:1.27, team/app)은 Docker Hub 로 본다.
DOCKER_HUB = "index.docker.io"


class ImageCopyError(ProvisioningError):
    """crane 이 로그인·복사·조회에 실패했을 때. crane 이 설치되어 있지 않은 것 같은 환경 문제와 구분한다."""


class ManualCopyRequired(ProvisioningError):
    """자동으로 복사하지 못한 원본. 사람이 목적지 저장소에 직접 복사한 뒤 다시 실행해야 한다."""


# ---------------------------------------------------------------------------
# 이미지 주소
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    tag: str | None = None
    digest: str | None = None

    @property
    def repo_url(self) -> str:
        return f"{self.registry}/{self.repository}"

    @property
    def pull_ref(self) -> str:
        """복사할 때 쓰는 주소. digest 가 있으면 태그보다 digest 로 정확히 그 이미지를 가리킨다."""
        if self.digest:
            return f"{self.repo_url}@{self.digest}"
        return f"{self.repo_url}:{self.tag}" if self.tag else self.repo_url


def parse_image(ref: str) -> ImageRef:
    """`저장소주소/이름[:태그][@sha256:...]` 를 나눈다.

    저장소 주소가 없는 이름은 Docker Hub 로 본다: nginx:1.27 → index.docker.io/library/nginx:1.27,
    team/app → index.docker.io/team/app. 저장소 주소는 첫 조각에 `.` 이나 `:` 이 있거나 localhost 일 때만 인정한다.
    """
    text = ref.strip()
    digest = None
    if "@" in text:
        text, digest = text.split("@", 1)
        if not _DIGEST.match(digest):
            raise ProvisioningError("이미지 digest 형식이 올바르지 않습니다 (sha256:<64자리 16진수>)")
    if not text or text.startswith("/") or text.endswith("/"):
        raise ProvisioningError(f"이미지 주소에 이름이 없습니다: {ref.strip()}")
    registry, sep, rest = text.partition("/")
    if not (sep and ("." in registry or ":" in registry or registry == "localhost")):
        registry, rest = DOCKER_HUB, text
        docker_hub = True
    else:
        docker_hub = False
    tag = None
    if ":" in rest.rsplit("/", 1)[-1]:
        rest, tag = rest.rsplit(":", 1)
    if not rest:
        raise ProvisioningError(f"이미지 주소에 이름이 없습니다: {ref.strip()}")
    if docker_hub and "/" not in rest:
        rest = f"library/{rest}"  # Docker Hub 의 공식 이미지는 library/ 아래에 있다
    return ImageRef(registry=registry, repository=rest, tag=tag or None, digest=digest)


def parse_ecr_host(registry: str) -> tuple[str, str] | None:
    """ECR 저장소 주소면 (계정, 리전), 아니면 None."""
    match = _ECR_HOST.match(registry)
    return (match.group("account"), match.group("region")) if match else None


def destination_tag(source: ImageRef) -> str:
    if source.tag:
        return source.tag
    return f"copy-{source.digest.removeprefix('sha256:')[:12]}" if source.digest else "latest"


# ---------------------------------------------------------------------------
# 로그인 토큰 받기
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryLogin:
    registry: str
    username: str
    password: str = field(repr=False)  # 토큰. 출력에 나오지 않게 한다.


def _error_code(error: Exception) -> str:
    """오류에서 코드만 꺼낸다. 메시지에는 계정 정보가 섞일 수 있어 남기지 않는다."""
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code:
            return str(code)
    return type(error).__name__


def aws_credentials(raw: Mapping[str, Any]) -> dict[str, str | None]:
    """원본 AWS 키. 조사 모듈(assessment/aws.py)과 같은 이름을 받는다: aws_* 가 먼저, 예전 이름도 받는다."""

    def pick(name: str, required: bool = True) -> str | None:
        for key in (f"aws_{name}", name):
            if raw.get(key):
                return str(raw[key])
        if required:
            raise ProvisioningError(f"원본 AWS 키에 {name} 가 없습니다")
        return None

    return {
        "access_key_id": pick("access_key_id"),
        "secret_access_key": pick("secret_access_key"),
        "session_token": pick("session_token", required=False),
    }


def load_aws_credentials(ref: str) -> dict[str, str | None]:
    """조사 출력의 원본 AWS 키 이름표(secret://...)를 풀어 키를 꺼낸다."""
    if not ref.startswith("secret://"):
        raise ProvisioningError("원본 AWS 키는 secret:// 이름표만 지원합니다")
    from app.core.secrets import resolve  # 비밀 저장소. 값은 저장된 곳에서만 꺼낸다.

    try:
        raw = resolve(ref)
    except KeyError:
        raise ProvisioningError("원본 AWS 키 이름표를 찾지 못했습니다") from None
    except Exception as error:  # 저장소·암호화 키 문제. 종류만 남긴다.
        raise ProvisioningError(f"원본 AWS 키 이름표를 풀 수 없습니다 ({type(error).__name__})") from None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ProvisioningError("원본 AWS 키 이름표의 값이 JSON 이 아닙니다") from None
    return aws_credentials(raw)


def aws_ecr_login(
    registry: str,
    credentials: Mapping[str, str | None],
    *,
    client_factory: Callable[..., Any] | None = None,
) -> RegistryLogin:
    """AWS 키로 ECR 읽기 토큰을 받는다 (ecr:GetAuthorizationToken). 보통 12시간짜리 임시 토큰이다."""
    parsed = parse_ecr_host(registry)
    if parsed is None:
        raise ProvisioningError("ECR 주소가 아닙니다")
    account, region = parsed
    if client_factory is None:
        import boto3  # 조사 모듈이 이미 쓰는 의존성

        client_factory = boto3.client
    try:
        client = client_factory(
            "ecr",
            region_name=region,
            aws_access_key_id=credentials["access_key_id"],
            aws_secret_access_key=credentials["secret_access_key"],
            aws_session_token=credentials.get("session_token"),
        )
        data = client.get_authorization_token(registryIds=[account])["authorizationData"][0]
        username, _, password = base64.b64decode(data["authorizationToken"]).decode().partition(":")
    except Exception as error:
        raise ProvisioningError(f"ECR 로그인 토큰을 받지 못했습니다 ({_error_code(error)})") from None
    return RegistryLogin(registry=registry, username=username, password=password)


def _adc_token() -> str:
    """이 환경의 기본 Google 로그인(ADC)에서 액세스 토큰을 받는다.

    로컬이면 `gcloud auth application-default login` 으로 만든 로그인, 서버면 서버에 붙은 서비스 계정이다.
    Terraform(google provider)이 쓰는 로그인과 같은 신분이다.
    """
    try:
        import google.auth
        import google.auth.transport.urllib3
        import urllib3
    except ImportError:
        raise ProvisioningError(
            "google-auth[urllib3] 가 설치되어 있지 않습니다. `pip install -e .` 를 다시 실행하세요"
        ) from None
    try:
        credentials, _ = google.auth.default(scopes=list(GCP_SCOPES))
        credentials.refresh(google.auth.transport.urllib3.Request(urllib3.PoolManager()))
    except google.auth.exceptions.DefaultCredentialsError:
        raise ProvisioningError(
            "GCP 기본 로그인(ADC)을 찾지 못했습니다. `gcloud auth application-default login` 을 실행하세요"
        ) from None
    except Exception as error:
        raise ProvisioningError(f"GCP 토큰을 받지 못했습니다 ({type(error).__name__})") from None
    if not credentials.token:
        raise ProvisioningError("GCP 토큰이 비어 있습니다")
    return credentials.token


def gcp_login(registry: str, *, token_source: Callable[[], str] = _adc_token) -> RegistryLogin:
    """Artifact Registry 는 사용자 이름 oauth2accesstoken + 액세스 토큰으로 로그인한다."""
    return RegistryLogin(registry=registry, username="oauth2accesstoken", password=token_source())


AZ_TOKEN_TIMEOUT_S = 60.0
ACR_TOKEN_USER = "00000000-0000-0000-0000-000000000000"  # az acr login --expose-token 토큰과 쓰는 고정 사용자 이름


def _az_acr_token(registry: str) -> str:
    """`az login` 신분으로 ACR 액세스 토큰(약 3시간)을 받는다. Terraform(azurerm)이 쓰는 로그인과 같은 신분이다."""
    name = registry.partition(".")[0]  # --name 에는 레지스트리 이름만 준다 (myregistry.azurecr.io -> myregistry)
    try:
        done = subprocess.run(
            ["az", "acr", "login", "--name", name, "--expose-token", "--output", "tsv", "--query", "accessToken"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=AZ_TOKEN_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        raise ProvisioningError(
            "az CLI 를 찾지 못했습니다. Azure CLI 를 설치하고 `az login` 을 실행하세요 (https://aka.ms/installazurecli)"
        ) from None
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProvisioningError(f"ACR 토큰을 받지 못했습니다 ({type(error).__name__})") from None
    token = done.stdout.strip()
    if done.returncode != 0 or not token:
        raise ProvisioningError(
            "ACR 토큰을 받지 못했습니다. `az login` 이 되어 있는지, 이 로그인에 해당 레지스트리의 AcrPush 권한이 있는지 확인하세요"
        )
    return token


def azure_login(registry: str, *, token_source: Callable[[str], str] = _az_acr_token) -> RegistryLogin:
    """Azure Container Registry 는 고정 사용자 이름(전부 0인 GUID) + az 액세스 토큰으로 로그인한다."""
    return RegistryLogin(registry=registry, username=ACR_TOKEN_USER, password=token_source(registry))


# 목적지 클라우드마다 로그인 방법이 다르다. 새 목적지는 여기에 등록한다.
DESTINATION_LOGINS: dict[str, Callable[[str], RegistryLogin]] = {"gcp": gcp_login, "azure": azure_login}


def destination_login(provider: str, registry: str) -> RegistryLogin:
    login = DESTINATION_LOGINS.get(provider)
    if login is None:
        raise ProvisioningError(
            f"목적지 '{provider}' 는 이미지 복사 로그인이 아직 없습니다 (지원: {', '.join(sorted(DESTINATION_LOGINS))})"
        )
    return login(registry)


# ---------------------------------------------------------------------------
# 복사 실행
# ---------------------------------------------------------------------------


class ImageCopier(Protocol):
    def copy(self, source: str, destination: str, logins: Sequence[RegistryLogin]) -> str:
        """source 를 destination 으로 복사하고, 목적지에 올라간 이미지의 digest 를 돌려준다."""
        ...


def _redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _tail(text: str, secrets: Sequence[str], lines: int = 5) -> str:
    kept = [line[:300] for line in _redact(text, secrets).strip().splitlines()[-lines:]]
    return " | ".join(kept) or "(출력 없음)"


# crane 에 넘기는 환경변수. 본인 로그인(AWS_*, GOOGLE_* 등)이 섞여 들어가지 않게 필요한 것만 넘긴다.
_CRANE_ENV = ("PATH", "HOME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
              "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR")


class CraneCopier:
    """crane(go-containerregistry)으로 저장소에서 저장소로 바로 복사한다. 도커 데몬이 필요 없고 digest 가 유지된다."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_s: float = COPY_TIMEOUT_S,
        runner: Callable[..., Any] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.binary = binary or os.getenv("CRANE_BIN") or "crane"
        self.timeout_s = timeout_s
        self.runner = runner
        self.which = which

    def _executable(self) -> str:
        found = self.which(self.binary)
        if not found:
            raise ProvisioningError(
                f"{self.binary} 가 설치되어 있지 않습니다. 설치하거나(brew install crane) "
                "환경변수 CRANE_BIN 으로 경로를 지정하세요"
            )
        return found

    def _run(self, command: list[str], env: dict[str, str], secrets: Sequence[str], label: str, stdin: str | None = None) -> str:
        try:
            done = self.runner(command, env=env, input=stdin, capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            raise ImageCopyError(f"crane {label} 가 {self.timeout_s:.0f}초 안에 끝나지 않았습니다") from None
        except OSError as error:
            raise ImageCopyError(f"crane {label} 를 실행하지 못했습니다 ({type(error).__name__})") from None
        if done.returncode != 0:
            raise ImageCopyError(f"crane {label} 실패 (종료 코드 {done.returncode}): {_tail(done.stderr or '', secrets)}")
        return done.stdout or ""

    def copy(self, source: str, destination: str, logins: Sequence[RegistryLogin]) -> str:
        binary = self._executable()
        secrets = [login.password for login in logins]
        # 로그인 정보는 임시 폴더에만 저장해 본인 컴퓨터의 Docker 설정을 건드리지 않고, 끝나면 폴더째 지운다.
        with tempfile.TemporaryDirectory(prefix="alpha-crane-") as config_dir:
            env = {name: os.environ[name] for name in _CRANE_ENV if name in os.environ}
            env["DOCKER_CONFIG"] = config_dir
            for login in logins:
                # 비밀번호는 명령 인자가 아니라 표준 입력으로 넘긴다 (프로세스 목록에 노출되지 않게).
                self._run(
                    [binary, "auth", "login", login.registry, "-u", login.username, "--password-stdin"],
                    env, secrets, "auth login", stdin=login.password,
                )
            self._run([binary, "copy", source, destination], env, secrets, "copy")
            digest = self._run([binary, "digest", destination], env, secrets, "digest").strip()
        if not _DIGEST.match(digest):
            raise ImageCopyError("복사한 이미지의 digest 를 읽지 못했습니다")
        return digest

    def existing_digest(self, destination: str, logins: Sequence[RegistryLogin]) -> str | None:
        """목적지에 이미 있는 이미지의 digest. 없거나 읽지 못하면 None. (crane 이 없으면 오류)"""
        binary = self._executable()
        secrets = [login.password for login in logins]
        with tempfile.TemporaryDirectory(prefix="alpha-crane-") as config_dir:
            env = {name: os.environ[name] for name in _CRANE_ENV if name in os.environ}
            env["DOCKER_CONFIG"] = config_dir
            try:
                for login in logins:
                    self._run(
                        [binary, "auth", "login", login.registry, "-u", login.username, "--password-stdin"],
                        env, secrets, "auth login", stdin=login.password,
                    )
                digest = self._run([binary, "digest", destination], env, secrets, "digest").strip()
            except ImageCopyError:
                return None
        return digest if _DIGEST.match(digest) else None


# ---------------------------------------------------------------------------
# 전체 흐름
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransferResult:
    container_image: str  # 목적지 저장소의 digest 로 고정한 주소. Terraform 의 container_image 로 넘긴다.
    source: str
    destination: str
    digest: str
    # True 면 원본에서 복사한 것이 아니라 사람이 목적지에 미리 복사해 둔 이미지를 쓴 것이다.
    reused: bool = False


# Azure 는 역할(AcrPush) 부여가 퍼지는 데 시간이 걸려, 부여 직후의 push 가 권한 오류로 실패할 수 있다. 그때만 다시 시도한다.
PUSH_RETRY_PROVIDERS = frozenset({"azure"})
PUSH_ATTEMPTS = 4
PUSH_RETRY_WAIT_S = 20.0
_AUTH_ERROR = re.compile(r"unauthorized|denied|forbidden|\b40[13]\b", re.IGNORECASE)


def _copy_with_retry(
    copier: ImageCopier,
    source: str,
    destination: str,
    logins: Sequence[RegistryLogin],
    *,
    retry: bool,
    sleep: Callable[[float], None],
    say: Callable[[str], None],
) -> str:
    attempts = PUSH_ATTEMPTS if retry else 1
    for attempt in range(1, attempts + 1):
        try:
            return copier.copy(source, destination, logins)
        except ImageCopyError as error:
            if attempt == attempts or not _AUTH_ERROR.search(str(error)):
                raise
            say(f"목적지 저장소 권한이 아직 퍼지지 않은 것 같아 {PUSH_RETRY_WAIT_S:g}초 뒤 다시 시도합니다 ({attempt}/{attempts - 1})")
            sleep(PUSH_RETRY_WAIT_S)
    raise AssertionError("unreachable")


def transfer_image(
    *,
    source_image: str,
    service_name: str,
    registry_url: str,
    provider: str,
    aws_credentials: Mapping[str, str | None] | None = None,
    copier: ImageCopier | None = None,
    source_login: Callable[[str, Mapping[str, str | None]], RegistryLogin] = aws_ecr_login,
    dest_login: Callable[[str, str], RegistryLogin] = destination_login,
    report: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> TransferResult:
    """원본 이미지를 `<registry_url>/<service_name>` 으로 복사하고 digest 로 고정한 주소를 돌려준다.

    목적지 주소 규칙은 Terraform 의 기본값(`<registry_url>/<service_name>:latest`)과 같은 저장소 아래다.

    자동으로 복사할 수 있는 원본은 ECR(원본 AWS 키 필요)과 로그인이 필요 없는 공개 저장소다. 그 밖의 비공개 저장소는
    열쇠를 받을 곳이 아직 없어서 복사에 실패한다. 이때 "이미지가 있다고 보고 진행"하면 나중에 배포에서 뒤늦게
    실패하므로, 목적지에 같은 이름의 이미지가 이미 있으면 그것을 쓰고 없으면 `ManualCopyRequired` 로 멈춰 사람이 복사하게 한다.
    """
    say = report or (lambda message: None)
    source = parse_image(source_image)
    registry_url = registry_url.strip().rstrip("/")
    dest_registry = registry_url.partition("/")[0]
    if not dest_registry or "/" not in registry_url:
        raise ProvisioningError("목적지 저장소 주소(registry_url)가 올바르지 않습니다")
    destination_repo = f"{registry_url}/{service_name}"
    destination = f"{destination_repo}:{destination_tag(source)}"

    is_ecr = parse_ecr_host(source.registry) is not None
    logins: list[RegistryLogin] = []
    if is_ecr:
        if not aws_credentials:
            raise ProvisioningError("원본이 ECR 인데 원본 AWS 키가 없어 이미지를 읽을 수 없습니다")
        say("원본 ECR 로그인")
        logins.append(source_login(source.registry, aws_credentials))
    say("목적지 저장소 로그인")
    destination_login_info = dest_login(provider, dest_registry)
    logins.append(destination_login_info)

    say("이미지 복사")
    image_copier = copier or CraneCopier()
    reused = False
    try:
        digest = _copy_with_retry(
            image_copier,
            source.pull_ref,
            destination,
            logins,
            retry=provider in PUSH_RETRY_PROVIDERS,
            sleep=sleep,
            say=say,
        )
    except ImageCopyError as error:
        if is_ecr:
            raise  # 열쇠를 알고 있는 원본이 실패한 것이므로 그대로 알린다
        lookup = getattr(image_copier, "existing_digest", None)
        existing = lookup(destination, [destination_login_info]) if lookup else None
        if not existing:
            raise ManualCopyRequired(
                f"원본 이미지({source.pull_ref})를 자동으로 복사하지 못했습니다. 로그인이 필요한 비공개 저장소일 수 있습니다. "
                f"이미지를 목적지 저장소에 직접 복사한 뒤 같은 작업을 다시 실행하세요 (목적지에 있는 이미지를 그대로 씁니다): "
                f"crane copy {source.pull_ref} {destination} (사유: {error})"
            ) from None
        say("원본에서 복사하지 못해 목적지에 이미 있는 이미지를 씁니다")
        digest, reused = existing, True
    if source.digest and digest != source.digest:
        raise ProvisioningError(f"복사한 이미지의 digest 가 원본과 다릅니다 (원본 {source.digest}, 복사본 {digest})")
    return TransferResult(
        container_image=f"{destination_repo}@{digest}",
        source=source.pull_ref,
        destination=destination,
        digest=digest,
        reused=reused,
    )
