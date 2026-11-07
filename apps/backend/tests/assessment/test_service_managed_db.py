"""gcp · azure 원본이 service.run() 을 끝까지 지나고, 수집기가 읽은 리전·계정이 결과로 가는지 확인."""

import pytest

from app.features.assessment import db as db_part
from app.features.assessment import service
from app.features.assessment.schemas import AssessmentInput, DbAssessment, DbInfo, SourceDb

APP = {"cloud": "gcp", "app": {"service_name": "orders", "image_uri": "us-docker.pkg.dev/p/r/orders:1",
                              "container_port": 8080},
       "db": {"engine_version": "16.4"}, "region": "asia-northeast3"}


def _db_result(**kw):
    return DbAssessment(
        facts=DbInfo(engine="postgres", engine_version="16.4", database_name="orders", logical_replication=True),
        source_db=SourceDb(instance_id="orders-db", database="orders"),
        region=kw.get("region", "asia-northeast3"), account_id=kw.get("account_id", "my-project"))


@pytest.fixture
def fake_db(monkeypatch):
    seen = {}

    def collect(inp, session):
        seen["session"] = session
        return _db_result()

    monkeypatch.setattr(db_part, "collect", collect)
    return seen


@pytest.mark.parametrize("kind", ["gcp", "azure"])
def test_runs_without_aws_and_carries_region_and_account(fake_db, kind):
    out = service.run(AssessmentInput(source_kind=kind, target_region="asia-northeast3", db_instance_id="orders-db"))
    assert fake_db["session"] is None  # AWS 세션을 만들지 않음
    assert out.source.region == "asia-northeast3" and out.source.account_id == "my-project"
    assert out.source.db.instance_id == "orders-db"
    assert any("앱" in g and "조회하지 않음" in g for g in out.gaps)
    assert not out.plan.approvable  # 앱 이미지를 확인하지 못해 승인 불가


def test_manual_app_supplies_the_image(fake_db):
    out = service.run(AssessmentInput(source_kind="gcp", target_region="asia-northeast3",
                                      db_instance_id="orders-db", manual=APP))
    assert out.plan.target_infra.source_image == "us-docker.pkg.dev/p/r/orders:1"
    assert out.plan.target_infra.container_port == 8080
    assert out.source.region == "asia-northeast3"


def test_collector_crash_is_a_gap_not_an_exception(monkeypatch):
    def boom(inp, session):
        raise RuntimeError("secret-looking message")

    monkeypatch.setattr(db_part, "collect", boom)
    out = service.run(AssessmentInput(source_kind="azure", target_region="asia-northeast3"))
    assert not out.plan.approvable
    assert any("RuntimeError" in g for g in out.gaps)
    assert "secret-looking" not in out.model_dump_json()


def test_aws_path_still_uses_session_account(monkeypatch):
    from .fakes import ACCOUNT, session
    out = service.run(AssessmentInput(source_kind="aws", aws_credentials_ref="secret://r/c", aws_region="ap-northeast-2",
                                      target_region="asia-northeast3"), session=session())
    assert out.source.account_id == ACCOUNT and out.source.region == "ap-northeast-2"
