"""원본 DB 크기(db_memory_gib, db_vcpu)를 Azure·AWS 의 DB 등급과 디스크로 바꾼다."""

import re

import pytest

from app.features.provisioning.providers import (
    AWS,
    AWS_DB_CLASSES,
    AZURE,
    AZURE_DB_TIERS,
    AZURE_DISK_SIZES_GB,
    BURSTABLE_NOTE,
    aws_adapt,
    azure_adapt,
)
from app.features.provisioning.service import run
from app.features.provisioning.terraform import ProvisioningError

from .test_aws_provider import aws_input, aws_outputs
from .test_azure_provider import azure_input, azure_outputs
from .test_service import FakeContext, FakeTerraform


def tier_of(adapt, key, memory=None, vcpu=None, **extra):
    infra, notes = adapt({"db_memory_gib": memory, "db_vcpu": vcpu, "db_tier": "db-g1-small", **extra})
    return infra.get(key), infra, notes


# ---------------------------------------------------------------------------
# Azure: 원본 메모리 이상인 가장 작은 등급
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("memory", "expected"),
    [
        (0.5, "B_Standard_B1ms"),
        (1, "B_Standard_B1ms"),
        (2, "B_Standard_B1ms"),  # 경계: 같으면 그 등급
        (2.01, "B_Standard_B2s"),
        (4, "B_Standard_B2s"),
        (5, "B_Standard_B2ms"),
        (8, "B_Standard_B2ms"),
        (9, "GP_Standard_D4ds_v5"),
        (16, "GP_Standard_D4ds_v5"),
        (32, "GP_Standard_D8ds_v5"),
        (64, "GP_Standard_D16ds_v5"),
        (128, "GP_Standard_D32ds_v5"),
    ],
)
def test_azure_picks_the_smallest_tier_that_covers_the_source_memory(memory, expected):
    tier, _, notes = tier_of(azure_adapt, "db_tier", memory=memory)

    assert tier == expected
    assert any(expected in note for note in notes)


def test_azure_caps_at_the_largest_tier_and_says_so():
    tier, _, notes = tier_of(azure_adapt, "db_tier", memory=500)

    assert tier == "GP_Standard_D32ds_v5"
    assert any("모자라" in note for note in notes)


def test_a_gcp_tier_name_never_reaches_azure_even_when_the_size_is_unknown():
    tier, infra, notes = tier_of(azure_adapt, "db_tier")

    assert tier is None and "db_tier" not in infra  # Terraform 기본값(B_Standard_B1ms)을 쓴다
    assert notes == []


def test_azure_falls_back_to_the_vcpu_count_when_the_memory_is_unknown():
    tier, _, _ = tier_of(azure_adapt, "db_tier", vcpu=4)

    assert tier == "GP_Standard_D4ds_v5"


def test_the_memory_wins_over_the_vcpu_count_when_both_are_known():
    # 원본이 2 GiB 에 vCPU 2 개여도 메모리 기준이라 가장 작은 등급이다 (GCP 규칙과 같다)
    tier, _, _ = tier_of(azure_adapt, "db_tier", memory=2, vcpu=2)

    assert tier == "B_Standard_B1ms"


@pytest.mark.parametrize(
    ("wanted", "expected"),
    [(10, 32), (20, 32), (32, 32), (33, 64), (100, 128), (1000, 1024), (1025, 2048)],
)
def test_azure_disk_is_rounded_up_to_a_size_azure_accepts(wanted, expected):
    _, infra, _ = tier_of(azure_adapt, "db_tier", db_disk_size_gb=wanted)

    assert infra["db_disk_size_gb"] == expected


def test_azure_disk_beyond_the_maximum_is_capped_with_a_note():
    _, infra, notes = tier_of(azure_adapt, "db_tier", db_disk_size_gb=40000)

    assert infra["db_disk_size_gb"] == 32768
    assert any("32768" in note for note in notes)


def test_azure_disk_is_left_to_the_default_when_unknown():
    _, infra, _ = tier_of(azure_adapt, "db_tier", memory=4)

    assert "db_disk_size_gb" not in infra


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("memory", "expected"),
    [
        (0.5, "db.t4g.micro"),
        (1, "db.t4g.micro"),
        (2, "db.t4g.small"),
        (3, "db.t4g.medium"),
        (4, "db.t4g.medium"),
        (8, "db.t4g.large"),
        (9, "db.t4g.xlarge"),
        (32, "db.t4g.2xlarge"),
        (33, "db.m6g.4xlarge"),
        (128, "db.m6g.8xlarge"),
    ],
)
def test_aws_picks_the_smallest_class_that_covers_the_source_memory(memory, expected):
    tier, _, notes = tier_of(aws_adapt, "db_instance_class", memory=memory)

    assert tier == expected
    assert any(expected in note for note in notes)


def test_aws_caps_at_the_largest_class_and_says_so():
    tier, _, notes = tier_of(aws_adapt, "db_instance_class", memory=1000)

    assert tier == "db.m6g.8xlarge"
    assert any("모자라" in note for note in notes)


def test_a_gcp_tier_name_never_reaches_aws():
    _, infra, _ = tier_of(aws_adapt, "db_instance_class")

    assert "db_tier" not in infra and "db_instance_class" not in infra  # Terraform 기본값(db.t4g.small)


@pytest.mark.parametrize(("wanted", "expected"), [(10, 20), (20, 20), (21, 21), (100, 100), (100.2, 101)])
def test_aws_disk_is_at_least_the_gp3_minimum(wanted, expected):
    _, infra, _ = tier_of(aws_adapt, "db_instance_class", db_disk_size_gb=wanted)

    assert infra["db_disk_size_gb"] == expected


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapt", [azure_adapt, aws_adapt])
def test_the_callers_dictionary_is_not_modified(adapt):
    original = {"db_memory_gib": 4, "db_tier": "db-g1-small", "db_disk_size_gb": 50}
    snapshot = dict(original)

    adapt(original)

    assert original == snapshot


@pytest.mark.parametrize("adapt", [azure_adapt, aws_adapt])
@pytest.mark.parametrize("key", ["db_memory_gib", "db_vcpu", "db_disk_size_gb"])
def test_a_non_numeric_db_size_is_an_error(adapt, key):
    with pytest.raises(ProvisioningError, match=key):
        adapt({key: "많이"})


@pytest.mark.parametrize("table", [AZURE_DB_TIERS, AWS_DB_CLASSES], ids=["azure", "aws"])
def test_tiers_are_listed_from_small_to_large_so_the_first_match_is_the_smallest(table):
    memories = [memory for _, memory, _ in table]
    assert memories == sorted(set(memories))


def test_tier_names_have_the_shape_each_terraform_provider_expects():
    assert all(re.match(r"^(B|GP|MO)_Standard_[A-Za-z0-9_]+$", name) for name, _, _ in AZURE_DB_TIERS)
    assert all(re.match(r"^db\.[a-z0-9]+\.[a-z0-9]+$", name) for name, _, _ in AWS_DB_CLASSES)


def test_each_terraform_default_tier_is_the_first_entry_of_the_table():
    # 크기를 모르면 등급을 넘기지 않아 Terraform 기본값이 쓰인다. 기본값이 표의 어느 등급인지 어긋나지 않게 한다.
    azure_default = re.search(r'variable "db_tier" \{.*?default\s*=\s*"([^"]+)"', (AZURE.terraform_dir / "variables.tf").read_text(encoding="utf-8"), re.DOTALL)
    aws_default = re.search(r'variable "db_instance_class" \{.*?default\s*=\s*"([^"]+)"', (AWS.terraform_dir / "variables.tf").read_text(encoding="utf-8"), re.DOTALL)

    assert azure_default and azure_default.group(1) == AZURE_DB_TIERS[0][0]
    assert aws_default and aws_default.group(1) in {name for name, _, _ in AWS_DB_CLASSES}


def test_azure_disk_sizes_in_code_are_the_ones_the_terraform_accepts():
    text = (AZURE.terraform_dir / "variables.tf").read_text(encoding="utf-8")
    match = re.search(r"contains\(\[([0-9,\s]+)\],\s*var\.db_disk_size_gb\)", text)
    assert match, "infra/azure/variables.tf 에서 db_disk_size_gb 검증 목록을 찾지 못했습니다"

    assert tuple(int(n) for n in match.group(1).split(",") if n.strip()) == AZURE_DISK_SIZES_GB


# ---------------------------------------------------------------------------
# provisioning 전체 흐름: Terraform 변수에 실제로 실린다
# ---------------------------------------------------------------------------


def test_azure_provisioning_sends_the_translated_tier_and_disk_to_terraform():
    infra = {"service_name": "target-app", "db_memory_gib": 4, "db_vcpu": 2, "db_tier": "db-g1-small", "db_disk_size_gb": 50}
    tf, ctx = FakeTerraform(outputs=azure_outputs()), FakeContext()

    run(azure_input(target_infra=infra), ctx, terraform=tf)

    assert tf.var_values["db_tier"] == "B_Standard_B2s"
    assert tf.var_values["db_disk_size_gb"] == 64
    assert "db_memory_gib" not in tf.var_values and "db_vcpu" not in tf.var_values  # Terraform 변수가 아니다
    assert any("B_Standard_B2s" in (message or "") for _, _, message in ctx.calls)


def test_aws_provisioning_sends_the_translated_class_and_disk_to_terraform():
    infra = {"service_name": "target-app", "db_memory_gib": 2, "db_tier": "db-g1-small", "db_disk_size_gb": 50}
    tf = FakeTerraform(outputs=aws_outputs())

    run(aws_input(target_infra=infra), FakeContext(), terraform=tf)

    assert tf.var_values["db_instance_class"] == "db.t4g.small"
    assert tf.var_values["db_disk_size_gb"] == 50
    assert "db_tier" not in tf.var_values


def test_the_tier_does_not_change_between_the_infrastructure_step_and_the_app_step():
    # Azure 의 최대 연결 수는 서버를 만들 때 등급으로 정해지므로, 두 번째 apply 에서 등급이 달라지면 안 된다
    infra = {"service_name": "target-app", "db_memory_gib": 16}
    first, _ = azure_adapt(infra)
    second, _ = azure_adapt(infra)

    assert first["db_tier"] == second["db_tier"] == "GP_Standard_D4ds_v5"


# ---------------------------------------------------------------------------
# 버스트 가능 등급 안내, HA 와의 충돌 방지
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapt", "key", "memory"),
    [
        (azure_adapt, "db_tier", 2),  # B_Standard_B1ms
        (azure_adapt, "db_tier", 8),  # B_Standard_B2ms
        (aws_adapt, "db_instance_class", 4),  # db.t4g.medium
        (aws_adapt, "db_instance_class", 32),  # db.t4g.2xlarge
    ],
)
def test_a_burstable_tier_is_flagged_in_the_note_so_the_approver_knows(adapt, key, memory):
    tier, _, notes = tier_of(adapt, key, memory=memory)

    assert tier is not None
    assert any(tier in note and BURSTABLE_NOTE in note for note in notes)


@pytest.mark.parametrize(
    ("adapt", "key", "memory"),
    [
        (azure_adapt, "db_tier", 16),  # GP_Standard_D4ds_v5
        (azure_adapt, "db_tier", 128),  # GP_Standard_D32ds_v5
        (aws_adapt, "db_instance_class", 64),  # db.m6g.4xlarge
        (aws_adapt, "db_instance_class", 128),  # db.m6g.8xlarge
    ],
)
def test_a_fixed_performance_tier_is_not_flagged_as_burstable(adapt, key, memory):
    tier, _, notes = tier_of(adapt, key, memory=memory)

    assert tier is not None
    assert not any(BURSTABLE_NOTE in note for note in notes)


def test_every_azure_tier_whose_name_starts_with_b_is_the_burstable_edition():
    # Azure 는 Burstable 등급이 HA 를 지원하지 않는다. 표의 B_ 이름이 모두 안내 대상인지 본다.
    from app.features.provisioning.providers import _is_burstable

    assert [name for name, _, _ in AZURE_DB_TIERS if _is_burstable(name)] == [name for name, _, _ in AZURE_DB_TIERS if name.startswith("B_")]


def test_high_availability_is_not_sent_so_it_cannot_clash_with_a_burstable_tier():
    # infra/azure 는 db_high_availability 가 켜지면 B_ 등급을 거부한다. 어댑터가 HA 를 넘기기 시작하면 이 테스트가 실패해서
    # 변환 결과(B_)와 충돌하는지 먼저 따져 보게 한다.
    assert "db_high_availability" not in AZURE.known_variables
    infra, _ = azure_adapt({"db_memory_gib": 2})
    assert "db_high_availability" not in infra
