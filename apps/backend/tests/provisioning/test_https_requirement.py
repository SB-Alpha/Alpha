"""Worker 는 https 목적지만 받는다. 앱 주소가 https 가 아니면 Worker 에 알리기 전에 해결 방법과 함께 멈춘다."""

import json
import re
from pathlib import Path

import pytest

from app.features.provisioning.providers import AWS
from app.features.provisioning.service import deploy_app
from app.features.provisioning.terraform import ProvisioningError

from .test_service import FIXTURE, FakeContext, FakeTerraform, FakeWorker, fake_clock, make_app_input, with_app_url

ACCOUNT = "123456789012"


def outputs_with(url: str) -> dict:
    return with_app_url(json.loads(FIXTURE.read_text(encoding="utf-8")), url)


def run(url: str, *, update_worker_target: bool, provider: str = "gcp", worker=None, http_get=lambda u: 200):
    extra = {"project_id": ACCOUNT} if provider == "aws" else {}
    return deploy_app(
        make_app_input(update_worker_target=update_worker_target, provider=provider, **extra),
        FakeContext(),
        terraform=FakeTerraform(outputs=outputs_with(url)),
        http_get=http_get,
        sleep=lambda s: None,
        clock=fake_clock(),
        worker=worker,
        ingress_probe=lambda u, secret: 404,
    )


def test_an_http_address_is_refused_before_the_worker_is_told_and_names_the_fix_for_aws():
    worker = FakeWorker()

    with pytest.raises(ProvisioningError) as error:
        run("http://target-app-123.ap-northeast-2.elb.amazonaws.com", update_worker_target=True, provider="aws", worker=worker)

    message = str(error.value)
    assert "https" in message and "enable_https_front" in message and "update_worker_target" in message
    assert worker.set_calls == []  # Worker 는 건드리지 않았다


def test_the_check_runs_before_waiting_for_the_app_so_the_failure_is_immediate_and_clear():
    def must_not_be_called(url):
        raise AssertionError("https 가 아닌 주소는 상태 확인 전에 멈춰야 합니다")

    with pytest.raises(ProvisioningError, match="https"):
        run("http://x.example.com", update_worker_target=True, provider="aws", worker=FakeWorker(), http_get=must_not_be_called)


def test_other_providers_get_a_generic_hint():
    with pytest.raises(ProvisioningError) as error:
        run("http://app.example.com", update_worker_target=True, provider="gcp", worker=FakeWorker())

    assert "https" in str(error.value) and "enable_https_front" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://d111111abcdef8.cloudfront.net",
        "https://target-app-abc.a.run.app",
        "https://app.azurecontainerapps.io",
    ],
)
def test_an_https_address_goes_through_and_the_worker_is_told(url):
    worker = FakeWorker()

    result = run(url, update_worker_target=True, worker=worker)

    assert worker.set_calls == [url] and result.worker_target_updated is True


@pytest.mark.parametrize("url", ["http://localhost:8080", "http://127.0.0.1:8080"])
def test_http_is_allowed_for_localhost_like_the_worker_allows_it(url):
    worker = FakeWorker()

    run(url, update_worker_target=True, worker=worker)

    assert worker.set_calls == [url]


def test_an_http_address_is_fine_when_the_worker_is_not_told():
    # Worker 에 알리지 않는 배포(시험, 정적 UPSTREAM_TARGET 설정 등)에서는 요건이 없다
    result = run("http://target-app-123.ap-northeast-2.elb.amazonaws.com", update_worker_target=False, provider="aws")

    assert result.healthy is True and result.worker_target_updated is False


# ---------------------------------------------------------------------------
# AWS Terraform: https 앞단이 의도대로 구성되어 있는지 (코드와 Terraform 이 어긋나지 않게)
# ---------------------------------------------------------------------------

TF = AWS.terraform_dir


def read(*parts: str) -> str:
    return Path(TF, *parts).read_text(encoding="utf-8")


def test_the_https_front_is_on_by_default_because_the_worker_only_accepts_https():
    match = re.search(r'variable "enable_https_front" \{.*?default\s*=\s*(true|false)', read("variables.tf"), re.DOTALL)

    assert match and match.group(1) == "true"


def test_the_front_never_caches_responses_and_forwards_everything_to_the_app():
    text = read("modules", "https_front", "main.tf")

    assert "Managed-CachingDisabled" in text  # 요청마다 결과가 다른 앱이라 캐시하면 안 된다
    assert "Managed-AllViewer" in text
    for method in ("GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"):
        assert f'"{method}"' in text


def test_viewers_are_forced_to_https_and_the_origin_hop_matches_the_alb_which_has_no_certificate():
    text = read("modules", "https_front", "main.tf")

    assert 'viewer_protocol_policy   = "redirect-to-https"' in text
    assert "allow-all" not in text
    assert 'origin_protocol_policy   = "http-only"' in text  # ALB 에는 인증서가 없다
    assert "cloudfront_default_certificate = true" in text  # 도메인 없이 https 주소가 나오는 이유


def test_the_app_url_output_prefers_the_https_front_and_keeps_the_alb_address_separately():
    text = read("outputs.tf")

    assert "module.https_front[0].url, module.app[0].app_url" in text
    assert 'output "alb_url"' in text


def test_the_front_is_built_only_with_the_app_because_it_needs_the_alb():
    text = read("main.tf")
    block = text[text.index('module "https_front"'):]

    assert "var.deploy_app && var.enable_https_front" in block
    assert "module.app[0].alb_dns_name" in block


def test_the_alb_is_limited_to_cloudfront_only_when_the_front_is_on():
    main = read("main.tf")
    network = read("modules", "network", "main.tf")

    assert "alb_cloudfront_only = var.enable_https_front" in main
    assert "com.amazonaws.global.cloudfront.origin-facing" in network
    assert "prefix_list_ids" in network


# ---------------------------------------------------------------------------
# 비밀 헤더: ALB 는 우리 CloudFront 가 붙이는 값이 맞는 요청만 앱으로 전달한다
# ---------------------------------------------------------------------------


def test_cloudfront_stamps_every_origin_request_with_the_secret_header():
    text = read("modules", "https_front", "main.tf")

    assert "custom_header" in text and '"X-Origin-Verify"' in text
    assert "var.origin_verify_secret" in text


def test_the_alb_refuses_everything_without_the_header_when_the_guard_is_on():
    text = read("modules", "app", "main.tf")

    assert 'var.origin_guard ? "fixed-response" : "forward"' in text  # 기본 응답은 거절
    assert 'status_code  = "403"' in text
    assert 'http_header_name = "X-Origin-Verify"' in text  # 이 헤더가 맞을 때만 앱으로
    assert "var.origin_verify_secret" in text


def test_both_sides_use_the_same_header_name_and_the_same_secret():
    root = read("main.tf")
    front = read("modules", "https_front", "main.tf")
    app = read("modules", "app", "main.tf")

    name = re.search(r'name\s*=\s*"(X-[A-Za-z-]+)"', front).group(1)
    assert f'http_header_name = "{name}"' in app  # 이름이 어긋나면 ALB 가 모든 요청을 거절한다
    assert "random_password.origin_verify[0].result" in root
    assert root.count("origin_verify_secret") >= 2  # 앱(ALB)과 CloudFront 양쪽에 같은 값이 넘어간다


def test_the_secret_is_generated_in_the_root_so_it_is_the_same_in_both_apply_steps():
    root = read("main.tf")
    block = root[root.index('resource "random_password" "origin_verify"'):]
    block = block[: block.index("\n}\n")]

    assert "var.enable_https_front" in block  # deploy_app 에 의존하지 않아야 두 번의 apply 에서 값이 같다
    assert "deploy_app" not in block


def test_the_guard_is_off_together_with_the_front_so_a_plain_alb_still_forwards():
    root = read("main.tf")

    assert "origin_guard          = var.enable_https_front" in root
    text = read("modules", "app", "variables.tf")
    assert re.search(r'variable "origin_guard" \{[^}]*default\s*=\s*false', text, re.DOTALL)


def test_the_secret_variables_are_marked_sensitive():
    for path in (("modules", "app", "variables.tf"), ("modules", "https_front", "variables.tf")):
        text = read(*path)
        block = text[text.index('variable "origin_verify_secret"'):]
        assert "sensitive = true" in block[: block.index("}") + 1], path
