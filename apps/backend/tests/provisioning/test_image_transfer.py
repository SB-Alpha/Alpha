import base64
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.features.provisioning.image_transfer import (
    CraneCopier,
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


@pytest.mark.parametrize("bad", ["nginx", "nginx:latest", "", f"{ECR}/", f"{ECR}/app@sha256:short"])
def test_parse_image_rejects_addresses_it_cannot_copy(bad):
    with pytest.raises(ProvisioningError):
        parse_image(bad)


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


def test_a_missing_crane_binary_explains_how_to_install_it():
    copier = make_copier(FakeRunner(), which=lambda name: None)

    with pytest.raises(ProvisioningError, match="brew install crane"):
        copier.copy("src", "dst", LOGINS)


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
    tf = tf or FakeTerraform(outputs=with_app_url(json.loads(FIXTURE.read_text())))
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
    outputs = with_app_url(json.loads(FIXTURE.read_text()))
    outputs.pop("registry_url")
    with pytest.raises(ProvisioningError, match="registry_url"):
        deploy(app_input(), FakeTransfer(), tf=FakeTerraform(outputs=outputs))


def test_a_failed_copy_stops_before_terraform_plans_or_applies(aws_key):
    def broken(**kwargs):
        raise ProvisioningError("crane copy 실패")

    tf = FakeTerraform(outputs=with_app_url(json.loads(FIXTURE.read_text())))
    with pytest.raises(ProvisioningError, match="crane copy 실패"):
        deploy(app_input(), broken, tf=tf)

    assert "plan" not in tf.calls and "apply" not in tf.calls
