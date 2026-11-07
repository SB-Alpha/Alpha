import json

import pytest

from app.core.secrets import resolve
from app.features.provisioning.schemas import AppDeployInput
from app.features.provisioning.service import deploy_app, run
from app.features.provisioning.target import default_credentials_ref
from app.features.provisioning.target_secrets import store_target_secrets
from app.features.provisioning.terraform import ProvisioningError

from .test_service import FIXTURE, FakeContext, FakeTerraform, fake_clock, make_input, with_app_url

ADMIN_PASSWORD = "FAKE-ADMIN-PASSWORD-DO-NOT-LEAK"
VPN_KEY = "FAKE-VPN-KEY-DO-NOT-LEAK"


def sample_outputs() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def outputs_without(*names: str) -> dict:
    outputs = sample_outputs()
    for name in names:
        outputs.pop(name)
    return outputs


class RecordingPut:
    """put_secret 대신 어떤 이름과 값이 저장되려 했는지 기록하는 가짜."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.saved: list[tuple[str, dict]] = []

    def __call__(self, name, value):
        if self.fail:
            raise self.fail
        self.saved.append((name, value))
        return name


def missing_from_store(ref: str) -> bool:
    try:
        resolve(ref)
    except KeyError:
        return True
    return False


# ---------------------------------------------------------------------------
# db_migration 이 값을 꺼낼 수 있다 (실제 비밀 저장소, 임시 폴더)
# ---------------------------------------------------------------------------


def test_the_references_in_the_result_resolve_to_the_terraform_values():
    result = run(make_input(), FakeContext(), terraform=FakeTerraform())

    refs = result.target["credentials_ref"]
    assert resolve(refs["admin"]) == {"user": sample_outputs()["db_admin_user"]["value"], "password": ADMIN_PASSWORD}
    assert resolve(refs["cloud"]) == {"credentials_json": VPN_KEY}


def test_nothing_is_stored_under_a_name_other_than_the_one_in_the_result():
    run(make_input(run_id="mig-001"), FakeContext(), terraform=FakeTerraform())

    other = default_credentials_ref("mig-999")
    assert missing_from_store(other["admin"]) and missing_from_store(other["cloud"])


def test_existing_infrastructure_still_gets_secrets_for_a_new_run():
    # 바뀔 것이 없는 계획(이미 만들어진 인프라)이어도 새 run 의 이름표로는 값이 있어야 db_migration 이 시작된다
    result = run(make_input(run_id="mig-002"), FakeContext(), terraform=FakeTerraform(plan_exit=0))

    assert result.applied is False
    assert resolve(result.target["credentials_ref"]["admin"])["password"] == ADMIN_PASSWORD


def test_two_runs_on_the_same_infrastructure_keep_separate_names():
    first = run(make_input(run_id="mig-001"), FakeContext(), terraform=FakeTerraform())
    second = run(make_input(run_id="mig-002"), FakeContext(), terraform=FakeTerraform())

    assert first.target["credentials_ref"] != second.target["credentials_ref"]
    assert resolve(first.target["credentials_ref"]["admin"]) == resolve(second.target["credentials_ref"]["admin"])


# ---------------------------------------------------------------------------
# 값은 저장소로만 간다
# ---------------------------------------------------------------------------


def test_secret_values_never_reach_progress_messages_or_the_result():
    ctx = FakeContext()
    result = run(make_input(), ctx, terraform=FakeTerraform())

    everything = result.model_dump_json() + " ".join(message or "" for _, _, message in ctx.calls)
    assert ADMIN_PASSWORD not in everything
    assert VPN_KEY not in everything
    assert any("목적지 비밀 저장" in (message or "") for _, _, message in ctx.calls)


def test_the_injected_store_receives_the_names_and_values_in_the_shape_db_migration_expects():
    put = RecordingPut()
    run(make_input(run_id="mig-001"), FakeContext(), terraform=FakeTerraform(), put_secret=put)

    assert [name for name, _ in put.saved] == ["secret://mig-001/target-admin", "secret://mig-001/target-cloud"]
    admin, cloud = (value for _, value in put.saved)
    assert set(admin) == {"user", "password"}  # input_secrets 의 target-admin 형식
    assert set(cloud) == {"credentials_json"}  # input_secrets 의 target-cloud 형식


# ---------------------------------------------------------------------------
# 저장하지 않는 경우와 실패하는 경우
# ---------------------------------------------------------------------------


def test_dry_run_stores_nothing():
    put = RecordingPut()
    result = run(make_input(dry_run=True), FakeContext(), terraform=FakeTerraform(), put_secret=put)

    assert put.saved == []
    assert missing_from_store(result.target["credentials_ref"]["admin"])


def test_a_missing_admin_password_stops_before_anything_is_stored():
    put = RecordingPut()
    with pytest.raises(ProvisioningError) as error:
        run(make_input(), FakeContext(), terraform=FakeTerraform(outputs=outputs_without("db_admin_password")), put_secret=put)

    assert "db_admin_password" in str(error.value)
    assert put.saved == []  # VPN 용도 저장하지 않는다 (관리자 계정이 없으면 이 run 은 어차피 진행할 수 없다)


def test_a_missing_vpn_key_is_a_warning_and_the_admin_secret_is_still_stored():
    put = RecordingPut()
    result = run(make_input(), FakeContext(), terraform=FakeTerraform(outputs=outputs_without("vpn_credentials_json")), put_secret=put)

    assert [name.rsplit("/", 1)[1] for name, _ in put.saved] == ["target-admin"]
    assert any("target-cloud" in problem for problem in result.problems)


def test_a_target_cloud_secret_entered_by_the_user_is_kept_and_not_warned_about():
    from app.core.secrets import put_secret

    refs = default_credentials_ref("mig-001")
    put_secret(refs["cloud"], {"credentials_json": "USER-ENTERED-KEY"})

    result = run(make_input(run_id="mig-001"), FakeContext(), terraform=FakeTerraform(outputs=outputs_without("vpn_credentials_json")))

    assert resolve(refs["cloud"]) == {"credentials_json": "USER-ENTERED-KEY"}  # Terraform 에 키가 없으면 건드리지 않는다
    assert not any("target-cloud" in problem for problem in result.problems)
    assert resolve(refs["admin"])["password"] == ADMIN_PASSWORD


def test_terraform_values_overwrite_what_the_user_entered_for_the_same_run():
    from app.core.secrets import put_secret

    refs = default_credentials_ref("mig-001")
    put_secret(refs["admin"], {"user": "postgres", "password": "USER-ENTERED-PASSWORD"})
    put_secret(refs["cloud"], {"credentials_json": "USER-ENTERED-KEY"})

    run(make_input(run_id="mig-001"), FakeContext(), terraform=FakeTerraform())

    assert resolve(refs["admin"])["password"] == ADMIN_PASSWORD  # 새로 만든 DB 의 비밀번호만 맞다
    assert resolve(refs["cloud"]) == {"credentials_json": VPN_KEY}


@pytest.mark.parametrize("run_id", ["a/b", "mig/../x", "A-1", "mig_1", "1abc", "a", "a" * 32, ""])
def test_a_malformed_run_id_is_rejected_before_anything_is_stored(run_id):
    put = RecordingPut()
    with pytest.raises(ProvisioningError, match="run_id"):
        store_target_secrets(sample_outputs(), run_id=run_id, put=put)

    assert put.saved == []


def test_a_store_failure_names_the_reference_and_error_type_but_not_the_value():
    put = RecordingPut(fail=RuntimeError(f"cannot write {ADMIN_PASSWORD}"))
    with pytest.raises(ProvisioningError) as error:
        run(make_input(), FakeContext(), terraform=FakeTerraform(), put_secret=put)

    assert "target-admin" in str(error.value)
    assert "RuntimeError" in str(error.value)
    assert ADMIN_PASSWORD not in str(error.value)


def test_progress_stays_ordered_with_the_new_step():
    ctx = FakeContext()
    run(make_input(), ctx, terraform=FakeTerraform())

    progress = [p for _, p, _ in ctx.calls]
    assert progress == sorted(progress)


def test_empty_values_count_as_missing():
    outputs = sample_outputs()
    outputs["db_admin_password"]["value"] = ""
    with pytest.raises(ProvisioningError, match="db_admin_password"):
        store_target_secrets(outputs, run_id="mig-001", put=RecordingPut())


# ---------------------------------------------------------------------------
# app_deploy 는 비밀을 건드리지 않는다
# ---------------------------------------------------------------------------


def test_deploy_app_does_not_store_secrets():
    result = deploy_app(
        AppDeployInput(run_id="mig-003", project_id="example-project", target_infra={"service_name": "target-app"}, update_worker_target=False),
        FakeContext(),
        terraform=FakeTerraform(outputs=with_app_url(sample_outputs())),
        http_get=lambda url: 200,
        sleep=lambda s: None,
        clock=fake_clock(),
    )

    assert result.healthy is True
    refs = default_credentials_ref("mig-003")
    assert missing_from_store(refs["admin"]) and missing_from_store(refs["cloud"])
