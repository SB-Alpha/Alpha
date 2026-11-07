"""클라우드 보안 서비스 응답 모양을 흉내 낸 가짜로 경보 변환과 실패 분류를 확인합니다. 네트워크는 쓰지 않습니다."""

import json

import httpx
import pytest
from botocore.exceptions import ClientError

from app.features.security import sources
from app.features.security.sources import SourceError

AWS_KEYS = {"aws_access_key_id": "AKIAFAKEKEY", "aws_secret_access_key": "super-secret-aws"}
GCP_KEYS = {"credentials_json": json.dumps({"type": "service_account", "project_id": "alpha-target", "private_key": "pk-secret"})}
AZURE_KEYS = {"tenant_id": "t", "client_id": "c", "client_secret": "azure-secret", "subscription_id": "sub-1"}
SECRETS = ("super-secret-aws", "pk-secret", "azure-secret")


class FakeGuardDuty:
    def __init__(self, detectors=("d1",), findings=None, error=None):
        self.detectors = list(detectors)
        self.findings = findings or []
        self.error = error
        self.criteria = None

    def list_detectors(self):
        if self.error:
            raise self.error
        return {"DetectorIds": self.detectors}

    def list_findings(self, DetectorId, FindingCriteria, MaxResults):
        self.criteria = FindingCriteria
        return {"FindingIds": [f["Id"] for f in self.findings]}

    def get_findings(self, DetectorId, FindingIds):
        return {"Findings": [f for f in self.findings if f["Id"] in FindingIds]}


FINDING = {
    "Id": "abc", "Arn": "arn:aws:guardduty:ap-northeast-2:111:detector/d1/finding/abc", "Severity": 7.5,
    "Type": "CredentialAccess:RDS/AnomalousBehavior.SuccessfulLogin", "Title": "Unusual login to RDS",
    "UpdatedAt": "2026-11-08T01:00:00.000Z",
    "Resource": {"ResourceType": "RDSDBInstance", "RdsDbInstanceDetails": {"DbInstanceIdentifier": "db-alpha-target"}},
}


@pytest.mark.parametrize("score,band", [(1.0, "LOW"), (3.9, "LOW"), (4.0, "MEDIUM"), (7.0, "HIGH"), (8.9, "HIGH"), (9.0, "CRITICAL")])
def test_guardduty_severity_bands(score, band):
    assert sources.guardduty_severity(score) == band


def test_guardduty_findings_become_alerts_with_the_real_resource():
    fake = FakeGuardDuty(findings=[FINDING])
    alerts = sources.guardduty_alerts("ap-northeast-2", AWS_KEYS, client_factory=lambda s, r: fake)
    assert fake.criteria == {"Criterion": {"service.archived": {"Eq": ["false"]}}}
    [alert] = alerts
    assert alert.id == "aws:abc" and alert.severity == "HIGH" and alert.resource_type == "database"
    assert alert.resource_id == "db-alpha-target" and alert.region == "ap-northeast-2"


def test_guardduty_without_a_detector_is_not_enabled():
    with pytest.raises(SourceError) as exc:
        sources.guardduty_alerts("ap-northeast-2", AWS_KEYS, client_factory=lambda s, r: FakeGuardDuty(detectors=()))
    assert exc.value.status == "not_enabled"


def test_guardduty_access_denied_is_no_access_without_quoting_secrets():
    error = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "super-secret-aws denied"}}, "ListDetectors")
    with pytest.raises(SourceError) as exc:
        sources.guardduty_alerts("ap-northeast-2", AWS_KEYS, client_factory=lambda s, r: FakeGuardDuty(error=error))
    assert exc.value.status == "no_access"
    assert not any(s in exc.value.message for s in SECRETS)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_scc_active_findings_become_alerts():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"listFindingsResults": [{"finding": {
            "name": "organizations/1/sources/2/findings/f9", "category": "OPEN_FIREWALL", "severity": "HIGH",
            "resourceName": "//compute.googleapis.com/projects/alpha-target/global/firewalls/allow-all",
            "eventTime": "2026-11-08T01:00:00Z"}}]})

    alerts, project = sources.scc_alerts(GCP_KEYS, client=_client(handler), token_fn=lambda key: "tok")
    assert project == "alpha-target"
    assert "/v1/projects/alpha-target/sources/-/findings" in seen["url"] and "ACTIVE" in seen["url"]
    assert alerts[0].severity == "HIGH" and alerts[0].threat_category == "OPEN_FIREWALL"


@pytest.mark.parametrize("status,body,expected", [
    (403, "permission denied", "no_access"),
    (400, "Security Command Center API is not enabled", "not_enabled"),
    (500, "boom", "error"),
])
def test_scc_failures_are_classified(status, body, expected):
    with pytest.raises(SourceError) as exc:
        sources.scc_alerts(GCP_KEYS, client=_client(lambda r: httpx.Response(status, text=body)), token_fn=lambda k: "t")
    assert exc.value.status == expected
    assert not any(s in exc.value.message for s in SECRETS)


def test_defender_keeps_only_active_alerts():
    def handler(request):
        if "login.microsoftonline.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.url.params["api-version"] == sources.AZURE_ALERTS_API_VERSION
        return httpx.Response(200, json={"value": [
            {"name": "a1", "properties": {"status": "Active", "severity": "Medium", "alertType": "K8S_X",
                                          "alertDisplayName": "Suspicious", "resourceIdentifiers": [
                                              {"azureResourceId": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.App/containerApps/target-app"}]}},
            {"name": "a2", "properties": {"status": "Resolved", "severity": "High"}},
        ]})

    alerts, subscription = sources.defender_alerts(AZURE_KEYS, client=_client(handler))
    assert subscription == "sub-1"
    assert [a.id for a in alerts] == ["azure:a1"]
    assert alerts[0].resource_type == "container" and alerts[0].severity == "MEDIUM"


def test_defender_login_rejected_is_no_access():
    with pytest.raises(SourceError) as exc:
        sources.defender_alerts(AZURE_KEYS, client=_client(lambda r: httpx.Response(401, text="azure-secret")))
    assert exc.value.status == "no_access"
    assert "azure-secret" not in exc.value.message


def test_missing_keys_are_reported_per_cloud_and_never_counted_as_zero(monkeypatch):
    sources.reset_cache()
    monkeypatch.setattr(sources, "_credentials", lambda cloud: (_ for _ in ()).throw(
        SourceError("no_credentials", "키 없음")))
    alerts, statuses = sources.fetch_all(["ap-northeast-2"])
    assert alerts == []
    assert {(s.provider, s.status) for s in statuses} == {("aws", "no_credentials"), ("gcp", "no_credentials"),
                                                         ("azure", "no_credentials")}
    assert all(s.count is None for s in statuses)


def test_no_aws_region_is_no_scope(monkeypatch):
    sources.reset_cache()
    monkeypatch.setattr(sources, "_credentials", lambda cloud: (_ for _ in ()).throw(SourceError("no_credentials", "x")))
    _, statuses = sources.fetch_all([])
    assert next(s for s in statuses if s.provider == "aws").status == "no_scope"


def test_results_are_cached_for_a_minute(monkeypatch):
    sources.reset_cache()
    now = [100.0]
    calls = []
    monkeypatch.setattr(sources, "_clock", lambda: now[0])
    monkeypatch.setattr(sources, "_credentials", lambda cloud: calls.append(cloud) or (_ for _ in ()).throw(
        SourceError("no_credentials", "x")))
    sources.fetch_all(["ap-northeast-2"])
    sources.fetch_all(["ap-northeast-2"])
    assert calls.count("gcp") == 1
    now[0] += sources.CACHE_TTL_S + 1
    sources.fetch_all(["ap-northeast-2"])
    assert calls.count("gcp") == 2
    sources.reset_cache()
