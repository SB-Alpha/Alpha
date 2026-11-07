"""RDS 인스턴스 클래스 크기 표: 오타와 누락을 잡는다. 값 자체는 EC2 API 로 확인한 것이다 (rules.py 주석 참고)."""

import pytest

from app.features.assessment.rules import RDS_MEMORY_GIB, RDS_VCPU


def test_memory_and_vcpu_cover_the_same_classes():
    assert set(RDS_MEMORY_GIB) == set(RDS_VCPU)


def test_the_table_is_not_the_tiny_mvp_list_anymore():
    assert len(RDS_MEMORY_GIB) >= 100


@pytest.mark.parametrize(
    ("name", "memory", "vcpu"),
    [
        ("db.t3.micro", 1, 2),
        ("db.t4g.small", 2, 2),
        ("db.t3.medium", 4, 2),
        ("db.t4g.large", 8, 2),
        ("db.m5.large", 8, 2),
        ("db.m6g.xlarge", 16, 4),
        ("db.r5.large", 16, 2),
        ("db.r6g.2xlarge", 64, 8),
        ("db.m5.24xlarge", 384, 96),
        ("db.r5.24xlarge", 768, 96),
    ],
)
def test_known_classes_have_the_size_aws_documents(name, memory, vcpu):
    assert RDS_MEMORY_GIB[name] == memory
    assert RDS_VCPU[name] == vcpu


def test_every_class_name_has_the_db_family_size_shape():
    assert all(name.count(".") == 2 and name.startswith("db.") for name in RDS_MEMORY_GIB)


def test_general_purpose_and_memory_optimized_families_keep_their_memory_to_vcpu_ratio():
    # m 계열은 vCPU 당 4 GiB, r 계열은 vCPU 당 8 GiB 로 만들어진다. 표를 손으로 고치다 생기는 오타를 잡는 검사다.
    for name, memory in RDS_MEMORY_GIB.items():
        family = name.split(".")[1]
        if family.startswith("m"):
            assert memory == RDS_VCPU[name] * 4, name
        elif family.startswith("r"):
            assert memory == RDS_VCPU[name] * 8, name


def test_memory_never_shrinks_as_the_size_grows_within_a_family():
    order = ["micro", "small", "medium", "large", "xlarge", "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge", "24xlarge", "32xlarge", "48xlarge"]
    by_family: dict[str, list[tuple[int, float]]] = {}
    for name, memory in RDS_MEMORY_GIB.items():
        _, family, size = name.split(".")
        by_family.setdefault(family, []).append((order.index(size), memory))
    for family, rows in by_family.items():
        memories = [memory for _, memory in sorted(rows)]
        assert memories == sorted(memories), family
