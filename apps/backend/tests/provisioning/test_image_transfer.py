import base64
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.features.provisioning.image_transfer import (
    CraneCopier,
    ImageCopyError,
    ManualCopyRequired,
    RegistryLogin,
    TransferResult,
    aws_credentials,
    aws_ecr_login,
    destination_login,
    destination_tag,
    gcp_login,
    load_aws_credentials,
    parse_ecr_host,
    parse_image,
    transfer_image,
)
from app.features.provisioning.schemas import AppDeployInput
from app.features.provisioning.service import deploy_app
from app.features.provisioning.terraform import ProvisioningError

from .test_service import FIXTURE, FakeContext, FakeTerraform, fake_clock, with_app_url

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
ECR = "061292478449.dkr.ecr.ap-northeast-2.amazonaws.com"
AR = "asia-northeast3-docker.pkg.dev/example-project/target-app"
SECRET_PASSWORD = "SUPER-SECRET-TOKEN-VALUE"


# ---------------------------------------------------------------------------
# 이미지 주소
# ---------------------------------------------------------------------------


def test_parse_image_splits_registry_name_tag_and_digest():
    ref = parse_image(f"{ECR}/target-app:v1")
    assert (ref.registry, ref.repository, ref.tag, ref.digest) == (ECR, "target-app", "v1", None)
    assert ref.pull_ref == f"{ECR}/target-app:v1"


def test_parse_image_prefers_digest_when_both_are_given():
    ref = parse_image(f"{ECR}/target-app:v1@{DIGEST}")
    assert ref.tag == "v1" and ref.digest == DIGEST
    assert ref.pull_ref == f"{ECR}/target-app@{DIGEST}"  # 태그가 아니라 digest 로 정확히 그 이미지를 가리킨다


def test_parse_image_keeps_a_registry_port_and_nested_repository():
    ref = parse_image("localhost:5000/team/app:dev")
    assert (ref.registry, ref.repository, ref.tag) == ("localhost:5000", "team/app", "dev")


@pytest.mark.parametrize("bad", ["", "/app", "team/", f"{ECR}/", f"{ECR}/app@sha256:short", "nginx@sha256:short"])
def test_parse_image_rejects_addresses_it_cannot_copy(bad):
    with pytest.raises(ProvisioningError):
        parse_image(bad)


@pytest.mark.parametrize(
    ("text", "repository", "tag"),
    [
        ("nginx", "library/nginx", None),
        ("nginx:1.27", "library/nginx", "1.27"),
        ("library/nginx:1.27", "library/nginx", "1.27"),  # 이미 library/ 가 붙은 이름은 그대로
        ("team/app:2", "team/app", "2"),  # 조직 이미지에는 library/ 를 붙이지 않는다
    ],
)
def test_parse_image_treats_names_without_a_registry_as_docker_hub(text, repository, tag):
    ref = parse_image(text)

    assert (ref.registry, ref.repository, ref.tag) == ("index.docker.io", repository, tag)
    assert ref.pull_ref == f"index.docker.io/{repository}" + (f":{tag}" if tag else "")


def test_parse_image_keeps_a_docker_hub_digest_and_an_explicit_docker_io_host():
    assert parse_image(f"nginx@{DIGEST}").pull_ref == f"index.docker.io/library/nginx@{DIGEST}"
    assert parse_image("docker.io/team/app:1").registry == "docker.io"  # 저장소 주소를 직접 쓴 것은 그대로 존중


def test_parse_ecr_host_returns_account_and_region_only_for_ecr():
    assert parse_ecr_host(ECR) == ("061292478449", "ap-northeast-2")
    assert parse_ecr_host("asia-northeast3-docker.pkg.dev") is None
    assert parse_ecr_host("example.azurecr.io") is None


def test_destination_tag_keeps_the_source_tag_and_names_digest_only_copies():
    assert destination_tag(parse_image(f"{ECR}/app:v1")) == "v1"
    assert destination_tag(parse_image(f"{ECR}/app@{DIGEST}")) == "copy-" + "a" * 12
    assert destination_tag(parse_image(f"{ECR}/app")) == "latest"


# ---------------------------------------------------------------------------
# 원본 AWS 키
# ---------------------------------------------------------------------------


def test_aws_credentials_accepts_both_key_names_like_the_assessment_module():
    new = aws_credentials({"aws_access_key_id": "A", "aws_secret_access_key": "S", "aws_session_token": "T"})
    old = aws_credentials({"access_key_id": "A", "secret_access_key": "S"})

    assert new == {"access_key_id": "A", "secret_access_key": "S", "session_token": "T"}
    assert old == {"access_key_id": "A", "secret_access_key": "S", "session_token": None}


def test_missing_aws_key_error_names_the_field_but_never_a_value():
    with pytest.raises(ProvisioningError) as error:
        aws_credentials({"aws_access_key_id": "AKIA-SECRET-ID"})

    assert "secret_access_key" in str(error.value)
    assert "AKIA-SECRET-ID" not in str(error.value)


def test_load_aws_credentials_only_accepts_secret_references():
    with pytest.raises(ProvisioningError, match="secret://"):
        load_aws_credentials("profile:default")


def test_load_aws_credentials_resolves_the_reference(monkeypatch):
    monkeypatch.setattr("app.core.secrets.resolve", lambda ref: {"aws_access_key_id": "A", "aws_secret_access_key": "S"})

    assert load_aws_credentials("secret://run/source-cloud")["access_key_id"] == "A"


def test_load_aws_credentials_reports_an_unknown_reference_without_leaking(monkeypatch):
    def missing(ref):
        raise KeyError(ref)

    monkeypatch.setattr("app.core.secrets.resolve", missing)

    with pytest.raises(ProvisioningError, match="찾지 못했습니다"):
        load_aws_credentials("secret://run/source-cloud")


# ---------------------------------------------------------------------------
# 로그인 토큰
# ---------------------------------------------------------------------------


class FakeEcrClient:
    def __init__(self, token="AWS:ecr-password"):
        self.token = token
        self.requests: list[dict] = []

    def get_authorization_token(self, **kwargs):
        self.requests.append(kwargs)
        return {"authorizationData": [{"authorizationToken": base64.b64encode(self.token.encode()).decode()}]}


def test_aws_ecr_login_asks_for_the_source_account_in_the_source_region():
    client = FakeEcrClient()
    seen: dict = {}

    def factory(service, **kwargs):
        seen.update(service=service, **kwargs)
        return client

    login = aws_ecr_login(ECR, {"access_key_id": "A", "secret_access_key": "S", "session_token": None}, client_factory=factory)

    assert (login.registry, login.username, login.password) == (ECR, "AWS", "ecr-password")
    assert seen["service"] == "ecr" and seen["region_name"] == "ap-northeast-2"
    assert seen["aws_access_key_id"] == "A"
    assert client.requests == [{"registryIds": ["061292478449"]}]


def test_aws_ecr_login_failure_reports_only_the_error_code():
    class Denied(Exception):
        response = {"Error": {"Code": "AccessDeniedException", "Message": "user arn:aws:iam::1:user/x is not authorized"}}

    def factory(service, **kwargs):
        raise Denied()

    with pytest.raises(ProvisioningError) as error:
        aws_ecr_login(ECR, {"access_key_id": "A", "secret_access_key": "S"}, client_factory=factory)

    assert "AccessDeniedException" in str(error.value)
    assert "arn:aws" not in str(error.value)


def test_aws_ecr_login_refuses_a_non_ecr_address():
    with pytest.raises(ProvisioningError, match="ECR"):
        aws_ecr_login("example.azurecr.io", {"access_key_id": "A", "secret_access_key": "S"})


def test_gcp_login_uses_the_access_token_user_name():
    login = gcp_login("asia-northeast3-docker.pkg.dev", token_source=lambda: "gcp-token")

    assert (login.username, login.password) == ("oauth2accesstoken", "gcp-token")


def test_a_login_never_prints_its_password():
    assert SECRET_PASSWORD not in repr(RegistryLogin("r", "u", SECRET_PASSWORD))


def test_destination_without_a_login_method_says_what_is_supported():
    with pytest.raises(ProvisioningError, match=r"'nowhere'.*gcp"):
        destination_login("nowhere", "registry.example")


# ---------------------------------------------------------------------------
# crane 실행
# ---------------------------------------------------------------------------


class FakeRunner:
    """crane 을 실제로 실행하지 않는 가짜. 명령, 표준 입력, 환경변수를 기록한다."""

    def __init__(self, *, digest=DIGEST, fail_on=None, stderr=""):
        self.digest = digest
        self.fail_on = fail_on
        self.stderr = stderr
        self.calls: list[dict] = []

    def __call__(self, command, **kwargs):
        step = command[1]
        self.calls.append({"command": command, "input": kwargs.get("input"), "env": kwargs.get("env"),
                           "config_exists": Path(kwargs["env"]["DOCKER_CONFIG"]).is_dir()})
        if step == self.fail_on:
            return SimpleNamespace(returncode=1, stdout="", stderr=self.stderr)
        return SimpleNamespace(returncode=0, stdout=self.digest + "\n" if step == "digest" else "", stderr="")


def make_copier(runner, which=lambda name: "/usr/local/bin/crane"):
    return CraneCopier(runner=runner, which=which)


LOGINS = [RegistryLogin(ECR, "AWS", "ecr-password"), RegistryLogin("asia-northeast3-docker.pkg.dev", "oauth2accesstoken", SECRET_PASSWORD)]


def test_crane_logs_in_to_both_registries_then_copies_then_reads_the_digest():
    runner = FakeRunner()
    digest = make_copier(runner).copy(f"{ECR}/app@{DIGEST}", f"{AR}/app:v1", LOGINS)

    steps = [call["command"][1:3] if call["command"][1] == "auth" else call["command"][1:2] for call in runner.calls]
    assert steps == [["auth", "login"], ["auth", "login"], ["copy"], ["digest"]]
    assert runner.calls[2]["command"][1:] == ["copy", f"{ECR}/app@{DIGEST}", f"{AR}/app:v1"]
    assert digest == DIGEST


def test_passwords_go_through_stdin_and_never_through_the_command_line():
    runner = FakeRunner()
    make_copier(runner).copy("src", "dst", LOGINS)

    logins = [call for call in runner.calls if call["command"][1] == "auth"]
    assert [call["input"] for call in logins] == ["ecr-password", SECRET_PASSWORD]
    assert all("--password-stdin" in call["command"] for call in logins)
    for call in runner.calls:
        assert SECRET_PASSWORD not in " ".join(call["command"])
        assert "ecr-password" not in " ".join(call["command"])


def test_login_state_lives_in_a_temp_folder_that_is_removed_afterwards():
    runner = FakeRunner()
    make_copier(runner).copy("src", "dst", LOGINS)

    folders = {call["env"]["DOCKER_CONFIG"] for call in runner.calls}
    assert len(folders) == 1
    folder = Path(folders.pop())
    assert all(call["config_exists"] for call in runner.calls)
    assert not folder.exists()
    assert str(folder) != str(Path.home() / ".docker")


def test_crane_does_not_inherit_the_callers_cloud_logins(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/leak/me.json")
    runner = FakeRunner()
    make_copier(runner).copy("src", "dst", LOGINS)

    for call in runner.calls:
        assert "AWS_SECRET_ACCESS_KEY" not in call["env"]
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in call["env"]


def test_a_failing_copy_reports_the_tail_of_stderr_with_secrets_masked():
    runner = FakeRunner(fail_on="copy", stderr=f"line1\nunauthorized: bad token {SECRET_PASSWORD}\n")
    with pytest.raises(ProvisioningError) as error:
        make_copier(runner).copy("src", "dst", LOGINS)

    assert "crane copy 실패" in str(error.value)
    assert "unauthorized" in str(error.value)
    assert SECRET_PASSWORD not in str(error.value)
    assert "***" in str(error.value)


def test_a_failing_copy_is_an_image_copy_error_so_callers_can_tell_it_from_a_setup_problem():
    runner = FakeRunner(fail_on="copy", stderr="unauthorized")
    with pytest.raises(ImageCopyError):
        make_copier(runner).copy("src", "dst", LOGINS)


def test_a_missing_crane_binary_explains_how_to_install_it():
    copier = make_copier(FakeRunner(), which=lambda name: None)

    with pytest.raises(ProvisioningError, match="brew install crane") as error:
        copier.copy("src", "dst", LOGINS)

    assert not isinstance(error.value, ImageCopyError)  # 설치 문제는 "수동 복사"로 넘기지 않고 그대로 알린다


def test_existing_digest_logs_in_only_to_the_given_registry_and_reads_the_digest():
    runner = FakeRunner()
    digest = make_copier(runner).existing_digest(f"{AR}/app:v1", LOGINS[1:])

    assert digest == DIGEST
    assert [call["command"][1] for call in runner.calls] == ["auth", "digest"]
    assert runner.calls[1]["command"][1:] == ["digest", f"{AR}/app:v1"]
    assert runner.calls[0]["input"] == SECRET_PASSWORD


def test_existing_digest_is_none_when_the_image_is_not_there_or_unreadable():
    assert make_copier(FakeRunner(fail_on="digest", stderr="MANIFEST_UNKNOWN")).existing_digest("dst", LOGINS[1:]) is None
    assert make_copier(FakeRunner(digest="not-a-digest")).existing_digest("dst", LOGINS[1:]) is None
    assert make_copier(FakeRunner(fail_on="auth")).existing_digest("dst", LOGINS[1:]) is None


def test_existing_digest_still_reports_a_missing_crane_binary():
    with pytest.raises(ProvisioningError, match="brew install crane"):
        make_copier(FakeRunner(), which=lambda name: None).existing_digest("dst", LOGINS[1:])


def test_a_timeout_is_reported_as_an_error():
    def hang(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    with pytest.raises(ProvisioningError, match="끝나지 않았습니다"):
        make_copier(hang).copy("src", "dst", LOGINS)


def test_an_unreadable_digest_is_an_error():
    with pytest.raises(ProvisioningError, match="digest"):
        make_copier(FakeRunner(digest="not-a-digest")).copy("src", "dst", LOGINS)


# ---------------------------------------------------------------------------
# 전체 흐름
# ---------------------------------------------------------------------------


class FakeCopier:
    def __init__(self, digest=DIGEST):
        self.digest = digest
        self.calls: list[tuple] = []

    def copy(self, source, destination, logins):
        self.calls.append((source, destination, list(logins)))
        return self.digest


def fake_source_login(registry, credentials):
    return RegistryLogin(registry, "AWS", "src-token")


def fake_dest_login(provider, registry):
    return RegistryLogin(registry, "dest-user", f"{provider}-token")


def run_transfer(copier, **overrides):
    args = dict(
        source_image=f"{ECR}/target-app@{DIGEST}",
        service_name="target-app",
        registry_url=AR.rsplit("/", 1)[0] + "/target-app",
        provider="gcp",
        aws_credentials={"access_key_id": "A", "secret_access_key": "S"},
        copier=copier,
        source_login=fake_source_login,
        dest_login=fake_dest_login,
    )
    args.update(overrides)
    return transfer_image(**args)


def test_transfer_copies_by_digest_into_the_service_repository_and_pins_the_result():
    copier = FakeCopier()
    result = run_transfer(copier)

    registry_url = "asia-northeast3-docker.pkg.dev/example-project/target-app"
    assert copier.calls[0][0] == f"{ECR}/target-app@{DIGEST}"
    assert copier.calls[0][1] == f"{registry_url}/target-app:copy-{'a' * 12}"
    assert [login.registry for login in copier.calls[0][2]] == [ECR, "asia-northeast3-docker.pkg.dev"]
    assert result.container_image == f"{registry_url}/target-app@{DIGEST}"  # Cloud Run 이 digest 로 고정된 이미지를 쓴다
    assert result.digest == DIGEST


def test_transfer_refuses_a_copy_whose_digest_differs_from_the_source():
    with pytest.raises(ProvisioningError, match="digest 가 원본과 다릅니다"):
        run_transfer(FakeCopier(digest=OTHER_DIGEST))


def test_transfer_accepts_a_tag_only_source_and_trusts_the_copied_digest():
    result = run_transfer(FakeCopier(digest=OTHER_DIGEST), source_image=f"{ECR}/target-app:v1")

    assert result.destination.endswith("/target-app:v1")
    assert result.digest == OTHER_DIGEST


def test_an_ecr_source_without_a_key_stops_before_anything_is_copied():
    copier = FakeCopier()
    with pytest.raises(ProvisioningError, match="원본 AWS 키"):
        run_transfer(copier, aws_credentials=None)

    assert copier.calls == []


class FailingCopier:
    """복사는 항상 실패하는 가짜. 목적지에 이미 있는 이미지(existing)를 조회할 수 있다."""

    def __init__(self, existing=None, error="crane copy 실패 (종료 코드 1): UNAUTHORIZED"):
        self.existing = existing
        self.error = error
        self.lookups: list[tuple] = []

    def copy(self, source, destination, logins):
        raise ImageCopyError(self.error)

    def existing_digest(self, destination, logins):
        self.lookups.append((destination, list(logins)))
        return self.existing


def test_a_docker_hub_name_is_copied_without_any_source_login():
    copier = FakeCopier()
    run_transfer(copier, source_image="nginx:1.27", aws_credentials=None)

    source, _, logins = copier.calls[0]
    assert source == "index.docker.io/library/nginx:1.27"
    assert [login.registry for login in logins] == ["asia-northeast3-docker.pkg.dev"]  # 목적지 로그인만


def test_a_private_non_ecr_source_stops_with_manual_copy_instructions():
    copier = FailingCopier()
    with pytest.raises(ManualCopyRequired) as error:
        run_transfer(copier, source_image="registry.local/team/app:1", aws_credentials=None)

    message = str(error.value)
    assert "crane copy registry.local/team/app:1" in message
    assert f"{AR}/target-app:1" in message  # 사람이 복사해야 할 정확한 목적지 이름
    assert "UNAUTHORIZED" in message  # 실패 사유도 보인다
    assert "다시 실행" in message
    assert len(copier.lookups) == 1  # 멈추기 전에 목적지에 이미 있는지 확인했다


def test_an_image_a_person_already_copied_to_the_destination_is_used():
    copier = FailingCopier(existing=DIGEST)
    messages: list[str] = []
    result = run_transfer(copier, source_image="registry.local/team/app:1", aws_credentials=None, report=messages.append)

    assert result.reused is True
    assert result.container_image == f"{AR}/target-app@{DIGEST}"
    destination, logins = copier.lookups[0]
    assert destination == f"{AR}/target-app:1"
    assert [login.registry for login in logins] == ["asia-northeast3-docker.pkg.dev"]  # 목적지 로그인만 쓴다
    assert any("목적지에 이미 있는 이미지" in message for message in messages)


def test_a_wrong_image_already_in_the_destination_is_rejected_by_digest():
    copier = FailingCopier(existing=OTHER_DIGEST)
    with pytest.raises(ProvisioningError, match="digest 가 원본과 다릅니다"):
        run_transfer(copier, source_image=f"registry.local/team/app@{DIGEST}", aws_credentials=None)


def test_an_ecr_copy_failure_is_reported_as_is_and_never_turned_into_a_manual_step():
    copier = FailingCopier(existing=DIGEST)
    with pytest.raises(ImageCopyError) as error:
        run_transfer(copier)  # 기본 원본은 ECR

    assert not isinstance(error.value, ManualCopyRequired)
    assert copier.lookups == []


def test_a_copier_that_cannot_look_up_the_destination_also_ends_in_a_manual_step():
    class CopyOnly:
        def copy(self, source, destination, logins):
            raise ImageCopyError("crane copy 실패")

    with pytest.raises(ManualCopyRequired):
        run_transfer(CopyOnly(), source_image="registry.local/team/app:1", aws_credentials=None)


def test_a_non_ecr_source_is_copied_without_a_source_login():
    copier = FakeCopier()
    run_transfer(copier, source_image="registry.example.com/team/app:1", aws_credentials=None)

    assert [login.registry for login in copier.calls[0][2]] == ["asia-northeast3-docker.pkg.dev"]


def test_transfer_rejects_a_malformed_destination_registry():
    with pytest.raises(ProvisioningError, match="registry_url"):
        run_transfer(FakeCopier(), registry_url="no-slash")


def test_transfer_reports_each_step():
    steps: list[str] = []
    run_transfer(FakeCopier(), report=steps.append)

    assert steps == ["원본 ECR 로그인", "목적지 저장소 로그인", "이미지 복사"]


# ---------------------------------------------------------------------------
# deploy_app 연결
# ---------------------------------------------------------------------------

PINNED = f"asia-northeast3-docker.pkg.dev/example-project/target-app/target-app@{DIGEST}"


class FakeTransfer:
    """transfer_image 를 대신하는 가짜. 어떤 인자로 불렸는지 기록한다."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        kwargs["report"]("이미지 복사")
        return TransferResult(container_image=PINNED, source="s", destination="d", digest=DIGEST)


def app_input(**overrides) -> AppDeployInput:
    args = {
        "run_id": "mig-001",
        "project_id": "example-project",
        "target_infra": {"service_name": "target-app", "source_image": f"{ECR}/target-app@{DIGEST}"},
        "source_credentials_ref": "secret://mig-001/source-cloud",
        "update_worker_target": False,
    }
    args.update(overrides)
    return AppDeployInput(**args)


def deploy(inp, transfer, tf=None, ctx=None):
    tf = tf or FakeTerraform(outputs=with_app_url(json.loads(FIXTURE.read_text(encoding="utf-8"))))
    ctx = ctx or FakeContext()
    result = deploy_app(inp, ctx, terraform=tf, http_get=lambda url: 200, sleep=lambda s: None, clock=fake_clock(), transfer=transfer)
    return result, tf, ctx


@pytest.fixture
def aws_key(monkeypatch):
    monkeypatch.setattr("app.core.secrets.resolve", lambda ref: {"aws_access_key_id": "A", "aws_secret_access_key": "S"})


def test_deploy_app_copies_the_image_after_init_and_gives_terraform_the_pinned_address(aws_key):
    transfer = FakeTransfer()
    result, tf, _ = deploy(app_input(), transfer)

    assert tf.calls[:3] == ["init", "output", "plan"]  # init 이 끝난 뒤 저장소 주소를 읽고, 계획 전에 복사한다
    assert tf.var_values["container_image"] == PINNED
    assert transfer.calls[0]["registry_url"] == "asia-northeast3-docker.pkg.dev/example-project/target-app"
    assert transfer.calls[0]["service_name"] == "target-app"
    assert transfer.calls[0]["provider"] == "gcp"
    assert transfer.calls[0]["aws_credentials"]["access_key_id"] == "A"
    assert result.healthy is True


def test_progress_stays_ordered_when_the_copy_runs(aws_key):
    _, _, ctx = deploy(app_input(), FakeTransfer())

    progress = [p for _, p, _ in ctx.calls]
    assert progress == sorted(progress)
    assert any("이미지 복사" in (m or "") for _, _, m in ctx.calls)


def test_without_a_source_key_an_ecr_image_is_not_copied_like_before():
    transfer = FakeTransfer()
    _, tf, ctx = deploy(app_input(source_credentials_ref=None), transfer)

    assert transfer.calls == []
    assert "container_image" not in tf.var_values
    assert "output" not in tf.calls[:3]
    assert any("복사를 건너뜁니다" in (m or "") for _, _, m in ctx.calls)


@pytest.mark.parametrize("ref", ["profile:default", "aws/default", ""])
def test_a_key_reference_that_is_not_secret_style_skips_the_copy_instead_of_failing(ref):
    # 조사 모듈은 로컬 개발용 profile:<이름> 도 받는다. 서버에서는 풀 수 없으니 앱 배포를 막지 않고 건너뛴다.
    transfer = FakeTransfer()
    result, tf, ctx = deploy(app_input(source_credentials_ref=ref), transfer)

    assert transfer.calls == []
    assert "container_image" not in tf.var_values
    assert any("복사를 건너뜁니다" in (m or "") for _, _, m in ctx.calls)
    assert result.healthy is True


def test_a_docker_hub_name_no_longer_stops_the_deploy_and_is_copied_without_a_key():
    # #61 의 직접 입력 원본이 nginx:1.27 같은 이름을 줘도 배포가 멈추지 않는다.
    transfer = FakeTransfer()
    infra = {"service_name": "target-app", "source_image": "nginx:1.27"}
    result, tf, _ = deploy(app_input(target_infra=infra, source_credentials_ref=None), transfer)

    assert transfer.calls[0]["source_image"] == "nginx:1.27"
    assert transfer.calls[0]["aws_credentials"] is None
    assert tf.var_values["container_image"] == PINNED
    assert result.healthy is True


class ManualTransfer:
    """자동 복사에 실패해 사람이 복사해야 하는 경우를 흉내 낸다."""

    def __call__(self, **kwargs):
        raise ManualCopyRequired("원본 이미지(registry.local/team/app:1)를 자동으로 복사하지 못했습니다. crane copy ...")


def test_a_private_source_that_cannot_be_copied_stops_the_deploy_before_terraform_applies():
    infra = {"service_name": "target-app", "source_image": "registry.local/team/app:1"}
    tf = FakeTerraform(outputs=with_app_url(json.loads(FIXTURE.read_text(encoding="utf-8"))))

    with pytest.raises(ManualCopyRequired, match="자동으로 복사하지 못했습니다"):
        deploy(app_input(target_infra=infra, source_credentials_ref=None), ManualTransfer(), tf=tf)

    assert "apply" not in tf.calls  # 가짜 성공 없이 앱을 만들기 전에 멈춘다


def test_the_progress_message_says_when_an_existing_destination_image_was_used():
    class Reuse(FakeTransfer):
        def __call__(self, **kwargs):
            super().__call__(**kwargs)
            return TransferResult(container_image=PINNED, source="s", destination="d", digest=DIGEST, reused=True)

    _, _, ctx = deploy(app_input(target_infra={"service_name": "target-app", "source_image": "registry.local/team/app:1"},
                                 source_credentials_ref=None), Reuse())

    assert any("목적지에 있던 이미지를 사용" in (m or "") for _, _, m in ctx.calls)


def test_dry_run_never_copies():
    transfer = FakeTransfer()
    _, tf, _ = deploy(app_input(dry_run=True), transfer)

    assert transfer.calls == []
    assert "container_image" not in tf.var_values


def test_an_explicit_container_image_is_left_alone(aws_key):
    transfer = FakeTransfer()
    infra = {"service_name": "target-app", "source_image": f"{ECR}/target-app@{DIGEST}", "container_image": "x.io/mine@sha256:1"}
    _, tf, _ = deploy(app_input(target_infra=infra), transfer)

    assert transfer.calls == []
    assert tf.var_values["container_image"] == "x.io/mine@sha256:1"


def test_no_source_image_means_nothing_to_copy():
    transfer = FakeTransfer()
    deploy(app_input(target_infra={"service_name": "target-app"}), transfer)

    assert transfer.calls == []


def test_a_non_ecr_source_is_copied_even_without_a_key():
    transfer = FakeTransfer()
    infra = {"service_name": "target-app", "source_image": "registry.example.com/team/app:1"}
    deploy(app_input(target_infra=infra, source_credentials_ref=None), transfer)

    assert len(transfer.calls) == 1
    assert transfer.calls[0]["aws_credentials"] is None


def test_the_service_name_is_required_to_name_the_destination(aws_key):
    infra = {"source_image": f"{ECR}/target-app@{DIGEST}"}
    with pytest.raises(ProvisioningError, match="service_name"):
        deploy(app_input(target_infra=infra), FakeTransfer())


def test_a_registry_url_missing_from_the_terraform_outputs_is_an_error(aws_key):
    outputs = with_app_url(json.loads(FIXTURE.read_text(encoding="utf-8")))
    outputs.pop("registry_url")
    with pytest.raises(ProvisioningError, match="registry_url"):
        deploy(app_input(), FakeTransfer(), tf=FakeTerraform(outputs=outputs))


def test_a_failed_copy_stops_before_terraform_plans_or_applies(aws_key):
    def broken(**kwargs):
        raise ProvisioningError("crane copy 실패")

    tf = FakeTerraform(outputs=with_app_url(json.loads(FIXTURE.read_text(encoding="utf-8"))))
    with pytest.raises(ProvisioningError, match="crane copy 실패"):
        deploy(app_input(), broken, tf=tf)

    assert "plan" not in tf.calls and "apply" not in tf.calls


# ---------------------------------------------------------------------------
# Azure: 역할 부여 직후 push 재시도, DR 저장소 복사, 로그인이 없는 목적지
# ---------------------------------------------------------------------------


class FlakyCopier:
    """앞의 몇 번은 권한 오류로 실패하고 그 뒤에 성공하는 가짜."""

    def __init__(self, failures, error="crane copy 실패 (종료 코드 1): UNAUTHORIZED: authentication required"):
        self.failures = failures
        self.error = error
        self.attempts = 0

    def copy(self, source, destination, logins):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise ImageCopyError(self.error)
        return DIGEST


def test_azure_retries_a_permission_error_right_after_the_role_grant():
    waits: list[float] = []
    copier = FlakyCopier(failures=2)

    result = run_transfer(copier, provider="azure", sleep=waits.append)

    assert copier.attempts == 3
    assert waits == [20.0, 20.0]
    assert result.digest == DIGEST


def test_azure_gives_up_after_the_last_attempt():
    copier = FlakyCopier(failures=99)

    with pytest.raises(ImageCopyError):
        run_transfer(copier, provider="azure", sleep=lambda s: None)

    assert copier.attempts == 4


def test_azure_does_not_retry_an_error_that_is_not_about_permission():
    copier = FlakyCopier(failures=99, error="crane copy 실패 (종료 코드 1): MANIFEST_UNKNOWN")

    with pytest.raises(ImageCopyError):
        run_transfer(copier, provider="azure", sleep=lambda s: pytest.fail("기다리면 안 됩니다"))

    assert copier.attempts == 1


def test_gcp_never_retries():
    copier = FlakyCopier(failures=99)

    with pytest.raises(ImageCopyError):
        run_transfer(copier, provider="gcp", sleep=lambda s: pytest.fail("기다리면 안 됩니다"))

    assert copier.attempts == 1


def with_dr(outputs: dict, url="drregistry.azurecr.io") -> dict:
    return {**outputs, "dr_registry_url": {"value": url}}


def test_with_dr_enabled_the_same_image_is_copied_to_the_dr_registry_too(aws_key):
    transfer = FakeTransfer()
    tf = FakeTerraform(outputs=with_app_url(with_dr(json.loads(FIXTURE.read_text(encoding="utf-8")))))

    result, _, ctx = deploy(app_input(), transfer, tf=tf)

    assert [call["registry_url"] for call in transfer.calls] == [
        "asia-northeast3-docker.pkg.dev/example-project/target-app",
        "drregistry.azurecr.io",
    ]
    assert tf.var_values["container_image"] == PINNED  # 주 앱은 주 저장소 주소를 쓴다
    assert any("DR 저장소에도 복사 완료" in (m or "") for _, _, m in ctx.calls)
    assert result.healthy is True


def test_without_dr_nothing_extra_is_copied(aws_key):
    transfer = FakeTransfer()

    deploy(app_input(), transfer)

    assert len(transfer.calls) == 1


def test_a_dr_copy_with_a_different_digest_fails(aws_key):
    class Diverging(FakeTransfer):
        def __call__(self, **kwargs):
            result = super().__call__(**kwargs)
            if len(self.calls) == 2:
                return TransferResult(container_image=PINNED, source="s", destination="d", digest=OTHER_DIGEST)
            return result

    tf = FakeTerraform(outputs=with_app_url(with_dr(json.loads(FIXTURE.read_text(encoding="utf-8")))))

    with pytest.raises(ProvisioningError, match="DR"):
        deploy(app_input(), Diverging(), tf=tf)


def test_a_destination_without_a_registry_login_skips_the_copy_instead_of_failing(aws_key, monkeypatch):
    from app.features.provisioning import service

    monkeypatch.setattr(service, "DESTINATION_LOGINS", {"gcp": object()})
    transfer = FakeTransfer()

    _, tf, ctx = deploy(app_input(provider="azure"), transfer)

    assert transfer.calls == []
    assert "container_image" not in tf.var_values
    assert any("이미지 복사 로그인이 없어" in (m or "") for _, _, m in ctx.calls)
