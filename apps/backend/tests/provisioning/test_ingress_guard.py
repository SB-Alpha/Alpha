"""Cloud Run 직접 호출 차단 (이슈 #34): Worker 시크릿 주입과 배포 직후 실측. Terraform·네트워크는 부르지 않는다."""

import json
from pathlib import Path

import pytest

from app.features.provisioning import service
from app.features.provisioning.providers import AWS, AZURE, GCP, PROVIDERS
from app.features.provisioning.service import build_tfvars, deploy_app, ingress_guard_state
from app.features.provisioning.terraform import TerraformCli

from .test_service import FakeContext, FakeTerraform, fake_clock, make_app_input, with_app_url

FIXTURE = Path(__file__).parent / "fixtures" / "terraform_output.sample.json"
SECRET = "worker-secret-value-123"
URL = "https://target-app-abc.a.run.app"


def _tfvars(provider="gcp"):
    return build_tfvars({"service_name": "app"}, project_id="p", deploy_app=False, provider=provider)


# --- Terraform 변수 ---------------------------------------------------------------------------------


def test_gcp_turns_on_protect_ingress_only_when_the_backend_has_the_worker_secret(monkeypatch):
    monkeypatch.delenv("WORKER_SECRET", raising=False)
    assert "protect_ingress" not in _tfvars()
    monkeypatch.setenv("WORKER_SECRET", SECRET)
    values = _tfvars()
    assert values["protect_ingress"] is True


def test_the_secret_value_never_goes_into_the_tfvars(monkeypatch):
    monkeypatch.setenv("WORKER_SECRET", SECRET)
    assert SECRET not in json.dumps(_tfvars())


@pytest.mark.parametrize("name", [n for n in PROVIDERS if n != "gcp"])
def test_destinations_without_the_variable_never_get_it(monkeypatch, name):
    monkeypatch.setenv("WORKER_SECRET", SECRET)
    assert "protect_ingress" not in _tfvars(name)
    assert PROVIDERS[name].ingress_guard_variable is None


def test_terraform_gets_the_secret_through_the_environment_only(monkeypatch):
    monkeypatch.setenv("WORKER_SECRET", SECRET)
    monkeypatch.delenv("TF_VAR_worker_secret", raising=False)
    env = TerraformCli(Path("."))._env()
    assert env["TF_VAR_worker_secret"] == SECRET


def _tf_var(env):
    """Windows 는 환경변수 이름을 대문자로 돌려주므로 대소문자 없이 찾는다."""
    return {key.upper(): value for key, value in env.items()}.get("TF_VAR_WORKER_SECRET")


def test_an_explicit_tf_var_wins_and_nothing_is_added_without_a_secret(monkeypatch):
    monkeypatch.setenv("WORKER_SECRET", SECRET)
    monkeypatch.setenv("TF_VAR_worker_secret", "explicit")
    assert _tf_var(TerraformCli(Path("."))._env()) == "explicit"
    monkeypatch.delenv("WORKER_SECRET")
    monkeypatch.delenv("TF_VAR_worker_secret")
    assert _tf_var(TerraformCli(Path("."))._env()) is None


def test_the_gcp_terraform_declares_the_variables_the_backend_sets():
    root = Path(__file__).resolve().parents[4] / "infra" / "terraform" / "gcp"
    text = (root / "variables.tf").read_text(encoding="utf-8")
    assert f'variable "{GCP.ingress_guard_variable}"' in text and 'variable "worker_secret"' in text
    assert "sensitive   = true" in text.split('variable "worker_secret"')[1]


# --- 배포 직후 실측 -----------------------------------------------------------------------------------


def probe_of(without, with_secret=200):
    calls = []

    def probe(url, secret):
        calls.append(secret)
        return with_secret if secret else without

    probe.calls = calls
    return probe


@pytest.mark.parametrize("without,with_secret,secret,state", [
    (403, 200, SECRET, "SECURE"),
    (403, 404, SECRET, "SECURE"),  # 헤더가 통과했으면 응답 코드가 403 이 아닌 것으로 충분
    (403, 403, SECRET, "MISCONFIGURED"),  # Worker 도 막히는 설정
    (200, 200, SECRET, "EXPOSED"),
    (302, 200, None, "EXPOSED"),
    (403, 0, None, "SECURE"),  # 시크릿을 몰라 헤더를 못 붙여도 직접 호출이 403 이면 막힌 것
])
def test_ingress_guard_state(without, with_secret, secret, state):
    assert ingress_guard_state(URL, probe_of(without, with_secret), secret)[0] == state


@pytest.mark.parametrize("without", [0, 404, 500, 503])
def test_unknown_responses_are_not_judged(without):
    state, why = ingress_guard_state(URL, probe_of(without), SECRET)
    assert state is None and str(without) in why


def test_the_first_probe_never_sends_the_secret_and_the_url_is_the_root():
    seen = []
    ingress_guard_state(URL + "/", lambda url, secret: seen.append((url, secret)) or 403, SECRET)
    assert seen[0] == (URL + "/", None)


def _deploy(monkeypatch, probe, *, secret=SECRET):
    if secret:
        monkeypatch.setenv("WORKER_SECRET", secret)
    else:
        monkeypatch.delenv("WORKER_SECRET", raising=False)
    tf = FakeTerraform(outputs=with_app_url(json.loads(FIXTURE.read_text(encoding="utf-8"))))
    ctx = FakeContext()
    reported = []
    ctx.report = lambda state, progress, metrics=None, message=None: reported.append((metrics, message))
    result = deploy_app(make_app_input(), ctx, terraform=tf, http_get=lambda url: 200, sleep=lambda s: None,
                        clock=fake_clock(), ingress_probe=probe)
    return result, reported


def test_deploy_reports_the_measured_guard_state_as_a_metric(monkeypatch):
    result, reported = _deploy(monkeypatch, probe_of(403, 200))
    assert result.ingress_guard == "SECURE"
    assert any(m == {"ingress_guard": "SECURE"} for m, _ in reported)


def test_deploy_reports_exposed_when_the_open_app_answers_without_the_header(monkeypatch):
    result, reported = _deploy(monkeypatch, probe_of(200), secret=None)
    assert result.ingress_guard == "EXPOSED"
    assert any(m == {"ingress_guard": "EXPOSED"} for m, _ in reported)


def test_deploy_does_not_invent_a_state_when_it_cannot_tell(monkeypatch):
    result, reported = _deploy(monkeypatch, probe_of(0))
    assert result.ingress_guard is None
    assert not any(m and "ingress_guard" in m for m, _ in reported)
    assert any("판단하지 않음" in (msg or "") for _, msg in reported)


def test_the_secret_value_is_never_written_to_the_report(monkeypatch):
    _, reported = _deploy(monkeypatch, probe_of(403, 200))
    assert SECRET not in json.dumps(reported, ensure_ascii=False)


def test_the_default_probe_is_replaceable_in_tests_so_no_real_request_is_made(monkeypatch):
    # conftest 가 기본 프로브를 0 으로 바꿔 둔다. 이 보호가 빠지면 기존 deploy_app 테스트가 진짜 네트워크를 친다.
    assert service._default_ingress_probe(URL, None) == 0
