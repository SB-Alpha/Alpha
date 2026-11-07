"""승인 가능한 계획은 이전 실행(db_migration)을 실제로 시작할 수 있어야 한다 (문제 정의 A2).

원본 정보가 이전 실행의 입력 계약(db_migration SourceInput)을 채우지 못하면 승인 불가여야 한다.
"""

import pytest

from app.features.assessment import db as db_part
from app.features.assessment import rules, service
from app.features.assessment.plan import build
from app.features.assessment.schemas import (
    AssessmentInput,
    DbAssessment,
    DbInfo,
    PlanningInput,
    SourceApp,
    SourceCredentials,
    SourceDb,
    SourceInfo,
    SourceNetwork,
    TableInfo,
)
from app.features.db_migration.schemas import SourceInput

from .fakes import session
from .test_plan import _ready_input


def full_source(**changes) -> SourceInfo:
    base = dict(
        cloud="aws", account_id="123456789012", region="ap-northeast-2",
        db=SourceDb(host="db.example.internal", port=5432, database="app", version="16.4"),
        network=SourceNetwork(vpc_id="vpc-1", cidr="172.31.0.0/16", route_table_ids=["rtb-1"]),
        app=SourceApp(db_user="app"), tables=["public.items"],
        credentials_ref=SourceCredentials(cloud="secret://r/cloud", db_admin="secret://r/db"),
    )
    return SourceInfo(**{**base, **changes})


def plan_with(source: SourceInfo | None):
    inp = _ready_input()
    return build(PlanningInput(inventory=inp.inventory.model_copy(update={"source": source}), target=inp.target))


def check(plan, cid="source_executable"):
    return next((c for c in plan.checks if c.id == cid), None)


# --- 검사 자체 -----------------------------------------------------------------------------------------


def test_a_complete_aws_source_passes_and_stays_approvable():
    plan = plan_with(full_source())
    assert check(plan).result == "pass" and plan.approvable, plan.blocking


@pytest.mark.parametrize("cloud", ["gcp", "azure", "onprem", "manual"])
def test_other_clouds_are_blocked_even_when_everything_else_passes(cloud):
    plan = plan_with(full_source(cloud=cloud))
    assert [c.id for c in plan.checks if c.result != "pass"] == ["source_executable"]
    assert check(plan).result == "fail" and not plan.approvable
    assert cloud in check(plan).detail and "조사·계획까지" in check(plan).detail
    assert any("이전 실행 입력" in b for b in plan.blocking)


@pytest.mark.parametrize("change,missing", [
    ({"network": SourceNetwork(vpc_id="v", cidr="10.0.0.0/16", route_table_ids=[])}, "라우트 테이블"),
    ({"network": SourceNetwork(vpc_id="v", cidr=None, route_table_ids=["r"])}, "VPC 대역"),
    ({"network": SourceNetwork(cidr="10.0.0.0/16", route_table_ids=["r"])}, "VPC ID"),
    ({"db": SourceDb(database="app", port=5432)}, "DB 주소"),
    ({"db": SourceDb(host="h", port=5432)}, "DB 이름"),
    ({"db": SourceDb(host="h", database="app")}, "DB 포트"),  # 비어 있으면 null 로 넘어가 기본값 5432 가 안 붙는다
    ({"tables": []}, "이전할 테이블"),
    ({"tables": ["alpha_cdc.cutover_marker"]}, "이전할 테이블"),  # workflows 가 빼고 넘기면 빈 목록
    ({"account_id": None}, "계정 ID"),
    ({"region": None}, "리전"),
    ({"app": SourceApp()}, "앱 DB 사용자"),
    ({"credentials_ref": SourceCredentials(db_admin="secret://r/db")}, "AWS 자격 증명 참조"),
    ({"credentials_ref": SourceCredentials(cloud="secret://r/c")}, "DB 관리자 참조"),
])
def test_aws_source_missing_a_required_fact_is_unknown_and_blocks(change, missing):
    plan = plan_with(full_source(**change))
    assert check(plan).result == "unknown" and not plan.approvable
    assert missing in check(plan).detail
    with pytest.raises(Exception):  # 이 검사가 막는 원본은 실제 입력 검증에서도 실패한다
        src = full_source(**change).model_dump(mode="json")
        src["tables"] = [t for t in src["tables"] if not t.startswith("alpha_cdc.")]
        SourceInput.model_validate(src)


def test_a_source_that_passes_the_check_also_passes_the_real_contract():
    SourceInput.model_validate(full_source().model_dump(mode="json"))


def test_no_source_given_means_no_check_for_direct_plan_inputs():
    plan = plan_with(None)
    assert check(plan) is None and plan.approvable


def test_the_executable_clouds_are_declared_in_one_place():
    assert rules.EXECUTABLE_SOURCE_CLOUDS == ("aws",)


# --- 실제 계약과 맞는지: 승인 가능한 AWS 출력은 db_migration 입력 검증을 통과한다 ---------------------------------


INP = AssessmentInput(aws_credentials_ref="secret://run-1/source-cloud", db_admin_ref="secret://run-1/source-db-admin",
                      aws_region="ap-northeast-2", target_region="asia-northeast3")


def _aws_db(monkeypatch, network=None):
    def fake_db(inp, sess):
        return DbAssessment(
            facts=DbInfo(engine="postgres", engine_version="16.15", database_name="app", instance_class="db.t3.micro",
                         allocated_storage_gb=20, logical_replication=True,
                         tables=[TableInfo(name="work_items", has_primary_key=True)]),
            source_db=SourceDb(instance_id="db-1", host="db.example.internal", port=5432, database="app", version="16.15"),
            network=network or SourceNetwork(vpc_id="vpc-1", cidr="172.31.0.0/16", route_table_ids=["rtb-1"]),
        )

    monkeypatch.setattr(db_part, "collect", fake_db)


def test_an_approvable_aws_output_satisfies_the_db_migration_source_contract(monkeypatch):
    _aws_db(monkeypatch)
    out = service.run(INP, session=session())
    assert out.plan.approvable, out.plan.blocking
    parsed = SourceInput.model_validate(out.source.model_dump(mode="json"))  # 검증에 실패하면 이 줄에서 예외
    assert parsed.cloud == "aws" and parsed.network.route_table_ids == ["rtb-1"]


def test_an_aws_run_that_lost_the_network_facts_is_not_approvable(monkeypatch):
    _aws_db(monkeypatch, network=SourceNetwork(vpc_id="vpc-1"))  # 네트워크 조회가 실패해 대역·라우트 테이블이 비었다
    out = service.run(INP, session=session())
    assert not out.plan.approvable
    assert "라우트 테이블" in check(out.plan).detail
    with pytest.raises(Exception):  # 승인을 막지 않았다면 이 입력으로 이전을 시작할 때 실패했을 것
        SourceInput.model_validate(out.source.model_dump(mode="json"))


def test_the_source_cloud_travels_to_the_output():
    from .test_cloud_source import _azure_inp, _gcp_inp

    assert service._source_cloud(INP) == "aws"
    assert service._source_cloud(_gcp_inp()) == "gcp" and service._source_cloud(_azure_inp()) == "azure"
    manual = AssessmentInput(source_kind="manual", target_region="asia-northeast3",
                             manual={"cloud": " OnPrem ", "app": {"service_name": "a"}})
    assert service._source_cloud(manual) == "onprem"
