import json
from pathlib import Path

import pytest

from app.features.provisioning.schemas import AppDeployInput, ProvisioningInput
from app.features.provisioning.service import build_tfvars, deploy_app, run
from app.features.provisioning.terraform import ProvisioningError, summarize_plan

FIXTURE = Path(__file__).parent / "fixtures" / "terraform_output.sample.json"

CREATE_PLAN = {
    "resource_changes": [
        {"address": "module.app.google_cloud_run_v2_service.this", "change": {"actions": ["create"]}},
        {"address": "module.network.google_compute_network.this", "change": {"actions": ["no-op"]}},
    ]
}
REPLACE_DB_PLAN = {
    "resource_changes": [
        {"address": "module.database.google_sql_database_instance.this", "change": {"actions": ["delete", "create"]}},
    ]
}
APPLY_LINES = [
    "module.network.google_compute_network.this: Creating...",
    "module.network.google_compute_network.this: Creation complete after 21s [id=x]",
    "module.database.google_sql_database_instance.this: Still creating... [10s elapsed]",
    "module.database.google_sql_database_instance.this: Creating...",
    "module.database.google_sql_database_instance.this: Creation complete after 293s [id=y]",
]


class FakeContext:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    def report(self, state, progress, metrics=None, message=None):
        self.calls.append((state, progress, message))


class FakeTerraform:
    """terraform 을 실제로 실행하지 않는 가짜. 어떤 순서로 불렸는지와 변수 파일 내용을 기록한다."""

    def __init__(self, *, plan_exit=2, plan_json=None, outputs=None):
        self.plan_exit = plan_exit
        self.plan_json = CREATE_PLAN if plan_json is None else plan_json
        self.outputs = json.loads(FIXTURE.read_text(encoding="utf-8")) if outputs is None else outputs
        self.calls: list[str] = []
        self.var_values: dict | None = None

    def init(self, on_line=None):
        self.calls.append("init")

    def plan(self, var_file, plan_file, on_line=None):
        self.calls.append("plan")
        self.var_values = json.loads(Path(var_file).read_text(encoding="utf-8"))  # 임시 폴더가 사라지기 전에 읽어 둔다
        return self.plan_exit

    def show_plan(self, plan_file):
        self.calls.append("show")
        return self.plan_json

    def apply(self, plan_file, on_line=None):
        self.calls.append("apply")
        for line in APPLY_LINES:
            if on_line:
                on_line(line)

    def output_json(self):
        self.calls.append("output")
        return self.outputs


def make_input(**overrides) -> ProvisioningInput:
    args = {
        "run_id": "mig-001",
        "project_id": "example-project",
        "target_infra": {
            "region": "asia-northeast3",
            "service_name": "target-app",
            "db_name": "workitems",
            "source_image": None,  # 값이 없는 키는 변수 파일에서 빠져야 한다
            "not_a_terraform_variable": "x",  # 선언되지 않은 키도 빠져야 한다
        },
    }
    args.update(overrides)
    return ProvisioningInput(**args)


def with_app_url(outputs: dict, url: str = "https://target-app-abc.a.run.app") -> dict:
    return {**outputs, "app_url": {"sensitive": False, "type": "string", "value": url}}


# ---------------------------------------------------------------------------
# run (provisioning 단계)
# ---------------------------------------------------------------------------


def test_run_plans_checks_then_applies_the_saved_plan_and_returns_target():
    tf, ctx = FakeTerraform(), FakeContext()
    result = run(make_input(), ctx, terraform=tf)

    assert tf.calls == ["init", "plan", "show", "apply", "output"]
    assert result.applied is True
    assert result.changes == {"add": 1, "change": 0, "destroy": 0}
    assert result.target["db"]["private_ip"] == "10.244.0.3"
    assert result.target["credentials_ref"]["admin"] == "secret://mig-001/target-admin"
    assert result.problems == []


def test_run_does_not_apply_when_nothing_changes():
    tf = FakeTerraform(plan_exit=0)
    result = run(make_input(), FakeContext(), terraform=tf)

    assert tf.calls == ["init", "plan", "output"]  # 계획 상세(show)도 apply 도 부르지 않는다
    assert result.applied is False
    assert result.changes == {"add": 0, "change": 0, "destroy": 0}


def test_run_refuses_a_plan_that_deletes_existing_resources():
    # 서비스 이름이 달라서 "기존 Cloud SQL 을 지우고 새로 만드는" 계획이 나온 경우
    tf = FakeTerraform(plan_json=REPLACE_DB_PLAN)
    with pytest.raises(ProvisioningError) as error:
        run(make_input(), FakeContext(), terraform=tf)

    assert "google_sql_database_instance" in str(error.value)
    assert "apply" not in tf.calls


def test_allow_destroy_lets_a_destructive_plan_through():
    tf = FakeTerraform(plan_json=REPLACE_DB_PLAN)
    result = run(make_input(allow_destroy=True), FakeContext(), terraform=tf)

    assert "apply" in tf.calls
    assert result.changes == {"add": 1, "change": 0, "destroy": 1}


def test_dry_run_plans_but_never_applies():
    tf = FakeTerraform()
    result = run(make_input(dry_run=True), FakeContext(), terraform=tf)

    assert "apply" not in tf.calls
    assert result.applied is False
    assert result.changes["add"] == 1
    assert result.target["db"]["database"] == "workitems"


def test_variable_file_has_only_declared_variables_and_forced_flags():
    tf = FakeTerraform()
    run(make_input(), FakeContext(), terraform=tf)

    assert tf.var_values == {
        "region": "asia-northeast3",
        "service_name": "target-app",
        "db_name": "workitems",
        "project_id": "example-project",
        "deploy_app": False,  # provisioning 단계에서는 앱을 만들지 않는다
        "create_vpn_key": True,
    }


def test_project_id_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "from-env")
    tf = FakeTerraform()
    result = run(make_input(project_id=None), FakeContext(), terraform=tf)

    assert tf.var_values["project_id"] == "from-env"
    assert result.target["project_id"] == "from-env"


def test_missing_project_id_is_an_error(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    with pytest.raises(ProvisioningError, match="GCP_PROJECT_ID"):
        run(make_input(project_id=None), FakeContext(), terraform=FakeTerraform())


def test_progress_never_goes_backwards_and_stays_within_100():
    ctx = FakeContext()
    run(make_input(), ctx, terraform=FakeTerraform())

    progress = [p for _, p, _ in ctx.calls]
    assert progress == sorted(progress)
    assert all(0 <= p <= 100 for p in progress)
    assert all(state == "running" for state, _, _ in ctx.calls)  # 끝(succeeded)은 반환으로 알린다
    messages = " ".join(m or "" for _, _, m in ctx.calls)
    assert "Creation complete" in messages


def test_missing_terraform_output_becomes_a_provisioning_error():
    outputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del outputs["db_private_ip"]
    with pytest.raises(ProvisioningError, match="db_private_ip"):
        run(make_input(), FakeContext(), terraform=FakeTerraform(outputs=outputs))


def test_settings_that_would_break_cdc_are_reported_as_problems():
    outputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    outputs["db_ssl_mode"]["value"] = "ALLOW_UNENCRYPTED_AND_ENCRYPTED"
    ctx = FakeContext()
    result = run(make_input(), ctx, terraform=FakeTerraform(outputs=outputs))

    assert any("ENCRYPTED_ONLY" in problem for problem in result.problems)
    assert any("주의" in (message or "") for _, _, message in ctx.calls)


def test_secret_values_in_terraform_output_never_reach_the_result():
    result = run(make_input(), FakeContext(), terraform=FakeTerraform())
    text = result.model_dump_json()
    assert "FAKE-ADMIN-PASSWORD-DO-NOT-LEAK" not in text
    assert "FAKE-VPN-KEY-DO-NOT-LEAK" not in text


# ---------------------------------------------------------------------------
# deploy_app (app_deploy 단계)
# ---------------------------------------------------------------------------


def make_app_input(**overrides) -> AppDeployInput:
    base = make_input()
    # 기본은 Worker 를 건드리지 않는다. Worker 시험은 update_worker_target=True 로 따로 한다.
    args = {
        "run_id": base.run_id,
        "project_id": base.project_id,
        "target_infra": base.target_infra,
        "update_worker_target": False,
    }
    args.update(overrides)
    return AppDeployInput(**args)


class FakeWorker:
    """Worker 를 실제로 부르지 않는 가짜. 어떤 주소로 불렸는지 기록한다."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.set_calls: list[str] = []

    def set_target(self, url: str) -> None:
        if self.fail:
            raise ProvisioningError("Worker target 명령 실패 (HTTP 409)")
        self.set_calls.append(url)


def fake_clock():
    ticks = iter(range(0, 10_000))
    return lambda: float(next(ticks))


def test_deploy_app_turns_the_app_on_and_waits_until_ready():
    tf = FakeTerraform(outputs=with_app_url(json.loads(FIXTURE.read_text(encoding="utf-8"))))
    statuses = iter([0, 503, 200])  # 아직 연결 안 됨 → 시작 중 → 정상
    asked: list[str] = []

    def http_get(url):
        asked.append(url)
        return next(statuses)

    result = deploy_app(make_app_input(), FakeContext(), terraform=tf, http_get=http_get, sleep=lambda s: None, clock=fake_clock())

    assert tf.var_values["deploy_app"] is True
    assert tf.var_values["create_vpn_key"] is True  # 두 번째 apply 가 VPN 키를 지우면 안 된다
    assert asked == ["https://target-app-abc.a.run.app/health/ready"] * 3
    assert result.healthy is True
    assert result.app_url == "https://target-app-abc.a.run.app"
    assert result.target["app"]["url"] == "https://target-app-abc.a.run.app"


def test_deploy_app_fails_if_the_app_never_becomes_ready():
    tf = FakeTerraform(outputs=with_app_url(json.loads(FIXTURE.read_text(encoding="utf-8"))))
    with pytest.raises(ProvisioningError, match="200"):
        deploy_app(
            make_app_input(health_timeout_s=5),
            FakeContext(),
            terraform=tf,
            http_get=lambda url: 503,
            sleep=lambda s: None,
            clock=fake_clock(),
        )


def test_deploy_app_fails_if_terraform_produced_no_app_url():
    tf = FakeTerraform()  # 출력에 app_url 이 없다
    with pytest.raises(ProvisioningError, match="app_url"):
        deploy_app(make_app_input(), FakeContext(), terraform=tf, http_get=lambda url: 200, sleep=lambda s: None)


def test_deploy_app_points_the_worker_at_the_new_app_after_it_is_ready():
    tf, worker = FakeTerraform(outputs=with_app_url(json.loads(FIXTURE.read_text(encoding="utf-8")))), FakeWorker()
    result = deploy_app(
        make_app_input(update_worker_target=True),
        FakeContext(),
        terraform=tf,
        http_get=lambda url: 200,
        sleep=lambda s: None,
        worker=worker,
    )
    assert worker.set_calls == ["https://target-app-abc.a.run.app"]
    assert result.worker_target_updated is True


def test_deploy_app_does_not_touch_the_worker_when_the_app_is_not_ready():
    tf, worker = FakeTerraform(outputs=with_app_url(json.loads(FIXTURE.read_text(encoding="utf-8")))), FakeWorker()
    with pytest.raises(ProvisioningError):
        deploy_app(
            make_app_input(update_worker_target=True, health_timeout_s=5),
            FakeContext(),
            terraform=tf,
            http_get=lambda url: 503,
            sleep=lambda s: None,
            clock=fake_clock(),
            worker=worker,
        )
    assert worker.set_calls == []  # 준비 안 된 앱으로 트래픽을 돌리게 하면 안 된다


def test_deploy_app_fails_before_terraform_when_the_worker_is_not_configured(monkeypatch):
    monkeypatch.delenv("WORKER_URL", raising=False)
    tf = FakeTerraform(outputs=with_app_url(json.loads(FIXTURE.read_text(encoding="utf-8"))))
    with pytest.raises(ProvisioningError, match="WORKER_URL"):
        deploy_app(make_app_input(update_worker_target=True), FakeContext(), terraform=tf, http_get=lambda url: 200)
    assert tf.calls == []  # Cloud Run 을 만들기 전에 멈춘다


def test_deploy_app_reports_a_worker_failure_as_an_error():
    tf = FakeTerraform(outputs=with_app_url(json.loads(FIXTURE.read_text(encoding="utf-8"))))
    with pytest.raises(ProvisioningError, match="409"):
        deploy_app(
            make_app_input(update_worker_target=True),
            FakeContext(),
            terraform=tf,
            http_get=lambda url: 200,
            sleep=lambda s: None,
            worker=FakeWorker(fail=True),
        )


def test_deploy_app_dry_run_does_not_need_or_touch_the_worker(monkeypatch):
    monkeypatch.delenv("WORKER_URL", raising=False)
    tf = FakeTerraform()
    result = deploy_app(make_app_input(dry_run=True, update_worker_target=True), FakeContext(), terraform=tf)
    assert result.worker_target_updated is False


def test_deploy_app_dry_run_does_not_wait_for_the_app():
    tf = FakeTerraform()
    result = deploy_app(make_app_input(dry_run=True), FakeContext(), terraform=tf, http_get=lambda url: 1 / 0)

    assert "apply" not in tf.calls
    assert result.healthy is False
    assert result.applied is False


# ---------------------------------------------------------------------------
# 작은 함수들
# ---------------------------------------------------------------------------


def test_build_tfvars_drops_unknown_keys_and_nones_and_forces_flags():
    values = build_tfvars({"cpu": 2, "memory": None, "nope": 1}, project_id="p", deploy_app=True)
    assert values == {"cpu": 2, "project_id": "p", "deploy_app": True, "create_vpn_key": True}


def test_summarize_plan_counts_like_terraform_does():
    plan = {
        "resource_changes": [
            {"address": "a", "change": {"actions": ["create"]}},
            {"address": "b", "change": {"actions": ["update"]}},
            {"address": "c", "change": {"actions": ["delete", "create"]}},  # 교체 = 추가 1 + 삭제 1
            {"address": "d", "change": {"actions": ["delete"]}},
            {"address": "e", "change": {"actions": ["no-op"]}},
            {"address": "f", "change": {"actions": ["read"]}},
        ]
    }
    counts, deletions = summarize_plan(plan)
    assert counts == {"add": 2, "change": 1, "destroy": 2}
    assert deletions == ["c", "d"]
