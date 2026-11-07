"""이름·단위·경로 규칙 (assessment 문제 정의 A4·A5·B2·C). 클라우드와 네트워크는 부르지 않는다."""

import pytest

from app.features.assessment import app as app_part
from app.features.assessment import cloud_source
from app.features.assessment.rules import RESERVED_SECRET_SUFFIXES, gcp_name, secret_names
from app.features.assessment.schemas import AppInfo

# --- B2: 십진 단위(M·G)와 이진 단위(Mi·Gi)는 다르다 ------------------------------------------------------


@pytest.mark.parametrize("value,mib", [
    ("512Mi", 512), ("1Gi", 1024), ("1.5Gi", 1536), ("2Gi", 2048),
    ("2G", 1907), ("1G", 954), ("512M", 488), ("1000M", 954),  # 십진: 2e9 B ≈ 1907 MiB
    ("256", 256), ("x", None), ("", None),
])
def test_memory_units_decimal_and_binary(value, mib):
    assert cloud_source._memory_mib(value) == mib


def test_a_gigabyte_is_not_a_gibibyte():
    assert cloud_source._memory_mib("2G") < cloud_source._memory_mib("2Gi")


# --- A5: 짧은 이름으로 파생 이름이 GCP 한도에 못 미치면 안 된다 --------------------------------------------


@pytest.mark.parametrize("name", ["a", "b", "x", "1", "_", "", "---"])
def test_derived_service_account_id_is_long_enough(name):
    derived = gcp_name(name)
    assert len(derived + "-run") >= 6  # IAM 서비스 계정 ID 는 6자 이상
    assert derived[0].isalpha() and len(derived) <= 49


def test_names_that_were_fine_stay_the_same():
    assert gcp_name("Target_App") == "target-app"
    assert gcp_name("ab") == "ab"


# --- A4: 비밀 이름이 서로 겹치거나 Terraform 이 만드는 비밀과 겹치면 안 된다 ----------------------------------


def test_db_password_does_not_take_the_terraform_secret_id():
    names = secret_names("app", ["DB_PASSWORD", "API_KEY"])
    assert names["API_KEY"] == "app-api-key"  # 겹치지 않는 이름은 그대로
    assert names["DB_PASSWORD"] != "app-db-password"
    assert names["DB_PASSWORD"].startswith("app-db-password-")


@pytest.mark.parametrize("suffix", RESERVED_SECRET_SUFFIXES)
def test_every_terraform_owned_secret_is_reserved(suffix):
    env = suffix.upper().replace("-", "_")
    assert secret_names("svc", [env])[env] != f"svc-{suffix}"


def test_names_that_differ_only_by_case_or_separator_stay_distinct():
    names = secret_names("app", ["API_KEY", "api_key", "API-KEY"])
    assert len(set(names.values())) == 3


def test_hashed_name_that_is_already_taken_gets_another_name():
    # sha1("a_b")[:6] == "eb2980": the hashed name for a_b is the plain name of A_B_EB2980
    names = secret_names("app", ["A_B", "A_B_EB2980", "a_b"])
    assert len(set(names.values())) == 3
    assert names["A_B_EB2980"] == "app-a-b-eb2980"
    assert names["a_b"] == "app-a-b-eb2980-2"


def test_secret_names_are_stable_and_independent_of_input_order():
    one = secret_names("app", ["A_B", "a_b", "DB_PASSWORD"])
    two = secret_names("app", ["DB_PASSWORD", "a_b", "A_B"])
    assert one == two


def test_secret_names_only_contain_characters_secret_manager_accepts():
    names = secret_names("app", ["MY.KEY", "WEIRD KEY!", "한글"])
    for name in names.values():
        assert name and all(c.isascii() and (c.isalnum() or c == "-") for c in name)
    assert len(set(names.values())) == 3


def test_terraform_really_owns_the_reserved_secret_ids():
    # 예약 이름이 Terraform 코드와 어긋나면(이름을 바꿨는데 여기만 그대로면) 충돌 방지가 헛돈다
    from pathlib import Path

    secrets_tf = (Path(__file__).resolve().parents[4] / "infra/terraform/gcp/modules/secrets/main.tf").read_text(encoding="utf-8")
    for suffix in RESERVED_SECRET_SUFFIXES:
        assert f'"${{var.name}}-{suffix}"' in secrets_tf


# --- C: 헬스체크 경로의 쿼리·조각은 어떤 원본에서도 남기지 않는다 ------------------------------------------


@pytest.mark.parametrize("raw,clean", [
    ("/health?token=SENTINEL", "/health"),
    ("/health#frag", "/health"),
    ("/health?a=1#x", "/health"),
    ("/health/live", "/health/live"),
    ("/", "/"),
    ("?token=SENTINEL", None),
    ("", None),
    (None, None),
])
def test_clean_health_path(raw, clean):
    assert app_part.clean_health_path(raw) == clean


def test_gcp_source_drops_the_token_in_the_probe_path():
    svc = {"template": {"containers": [{"image": "x/y@sha256:a", "ports": [{"containerPort": 8080}],
                                        "startupProbe": {"httpGet": {"path": "/health?token=SENTINEL"}}}]}}
    info, _, _ = cloud_source._gcp_app(svc, "svc", [])
    assert info.health_check_path == "/health" and "SENTINEL" not in info.model_dump_json()


def test_azure_source_drops_the_token_in_the_probe_path():
    app = {"properties": {"configuration": {"ingress": {"targetPort": 8080}}, "template": {"containers": [{
        "image": "r.azurecr.io/x:1", "probes": [{"type": "Liveness", "httpGet": {"path": "/h?token=SENTINEL", "port": 8080}}]}]}}}
    info, _, _ = cloud_source._azure_app(app, "app", [])
    assert info.health_check_path == "/h" and "SENTINEL" not in info.model_dump_json()


def test_aws_target_group_path_drops_the_token():
    class Elb:
        def describe_target_groups(self, **_):
            return {"TargetGroups": [{"HealthCheckPath": "/ping?token=SENTINEL"}]}

    class Session:
        def client(self, name):
            return Elb()

    service = {"loadBalancers": [{"targetGroupArn": "arn:x"}]}
    call = lambda what, fn, **kw: fn(**kw)  # noqa: E731
    assert app_part._health_from_target_group(service, Session(), call) == "/ping"


def test_the_token_never_reaches_the_ai_prompt():
    from app.features.assessment import ai
    from app.features.assessment.plan import build

    from .test_plan import _ready_input

    inp = _ready_input()
    app = inp.inventory.app.model_copy(update={"health_check_path": app_part.clean_health_path("/health?token=SENTINEL")})
    plan = build(inp.model_copy(update={"inventory": inp.inventory.model_copy(update={"app": app})}))
    assert "SENTINEL" not in ai._plan_for_prompt(plan)
    assert isinstance(AppInfo(service_name="x").health_check_path, (str, type(None)))
