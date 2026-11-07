"""클라우드 보안 서비스에서 실제 경보를 읽습니다. 쓰기 호출은 하지 않습니다.

- AWS GuardDuty: ListDetectors → ListFindings(보관 안 된 것) → GetFindings. 권한: guardduty:ListDetectors·ListFindings·GetFindings
- GCP Security Command Center: v1 projects/{project}/sources/-/findings (state="ACTIVE"). 권한: roles/securitycenter.findingsViewer
- Azure Defender for Cloud: Microsoft.Security/alerts (status Active). 권한: Security Reader

키는 DB 이전 화면이 등록한 secret://cloud/{cloud} 를 그대로 씁니다 (db_migration run_input.CLOUD_CREDENTIAL_KEYS).
메시지에는 비밀값·응답 원문을 넣지 않고, 확인하지 못한 클라우드는 이유(status)를 따로 돌려줍니다.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from .schemas import AlertSeverity, ResourceType, SecurityAlert, SourceStatus

log = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 10
MAX_FINDINGS = 50
CACHE_TTL_S = 60
AZURE_ALERTS_API_VERSION = "2022-01-01"
GCP_SCC = "https://securitycenter.googleapis.com/v1"
AZURE_ARM = "https://management.azure.com"


class SourceError(Exception):
    """status 와 짧은 메시지만 담습니다."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _credentials(cloud: str) -> dict[str, str]:
    from app.core.secrets import has_secret, resolve

    ref = f"secret://cloud/{cloud}"
    if not has_secret(ref):
        raise SourceError("no_credentials", "DB 이전 화면(클라우드 키)에 키가 등록되지 않았습니다")
    return resolve(ref)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _http_status(status_code: int, what: str) -> SourceError:
    if status_code in (401, 403):
        return SourceError("no_access", f"{what}: 읽기 권한이 없습니다 (HTTP {status_code})")
    return SourceError("error", f"{what}: HTTP {status_code}")


# ---- AWS GuardDuty --------------------------------------------------------------------------

def guardduty_severity(score: Any) -> AlertSeverity:
    """GuardDuty 심각도 점수 구간: Low 1.0~3.9, Medium 4.0~6.9, High 7.0~8.9, Critical 9.0~10.0."""
    value = float(score) if isinstance(score, (int, float)) else 0.0
    if value >= 9.0:
        return "CRITICAL"
    if value >= 7.0:
        return "HIGH"
    if value >= 4.0:
        return "MEDIUM"
    return "LOW"


_AWS_RESOURCE_TYPES: dict[str, ResourceType] = {
    "Instance": "vm",
    "EKSCluster": "container",
    "ECSCluster": "container",
    "Container": "container",
    "Lambda": "container",
    "RDSDBInstance": "database",
    "RDSLimitlessDB": "database",
    "AccessKey": "iam_role",
    "S3Bucket": "storage",
}


def _guardduty_resource_id(finding: dict) -> str:
    resource = finding.get("Resource") or {}
    rds = resource.get("RdsDbInstanceDetails") or {}
    ecs = resource.get("EcsClusterDetails") or {}
    instance = resource.get("InstanceDetails") or {}
    for value in (rds.get("DbInstanceIdentifier"), ecs.get("Arn") or ecs.get("Name"), instance.get("InstanceId")):
        if isinstance(value, str) and value:
            return value
    return str(finding.get("Arn") or finding.get("Id") or "unknown")


def guardduty_alerts(region: str, credentials: dict[str, str], client_factory: Callable[..., Any] | None = None) -> list[SecurityAlert]:
    from botocore.exceptions import BotoCoreError, ClientError

    if client_factory is None:
        import boto3
        from botocore.config import Config

        def client_factory(service: str, region_name: str) -> Any:
            return boto3.client(
                service,
                region_name=region_name,
                aws_access_key_id=credentials["aws_access_key_id"],
                aws_secret_access_key=credentials["aws_secret_access_key"],
                config=Config(connect_timeout=5, read_timeout=HTTP_TIMEOUT_S, retries={"max_attempts": 2}),
            )

    client = client_factory("guardduty", region)
    try:
        detectors = client.list_detectors().get("DetectorIds", [])
        if not detectors:
            raise SourceError("not_enabled", f"GuardDuty 가 {region} 에서 켜져 있지 않습니다")
        alerts: list[SecurityAlert] = []
        for detector in detectors:
            ids = client.list_findings(
                DetectorId=detector,
                FindingCriteria={"Criterion": {"service.archived": {"Eq": ["false"]}}},
                MaxResults=MAX_FINDINGS,
            ).get("FindingIds", [])
            if not ids:
                continue
            for f in client.get_findings(DetectorId=detector, FindingIds=ids[:MAX_FINDINGS]).get("Findings", []):
                resource_type = (f.get("Resource") or {}).get("ResourceType", "")
                alerts.append(SecurityAlert(
                    id=f"aws:{f.get('Id', '')}",
                    provider="aws",
                    severity=guardduty_severity(f.get("Severity")),
                    resource_type=_AWS_RESOURCE_TYPES.get(resource_type, "other"),
                    resource_id=_guardduty_resource_id(f),
                    threat_category=str(f.get("Type") or "unknown"),
                    description=str(f.get("Title") or f.get("Description") or ""),
                    detected_at=_parse_time(f.get("UpdatedAt") or f.get("CreatedAt")),
                    region=region,
                ))
        return alerts
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation"):
            raise SourceError("no_access", "GuardDuty 읽기 권한이 없습니다 (guardduty:List*/Get*)") from None
        raise SourceError("error", f"GuardDuty 호출 실패 ({code or type(exc).__name__})") from None
    except BotoCoreError as exc:
        raise SourceError("error", f"GuardDuty 연결 실패 ({type(exc).__name__})") from None


# ---- GCP Security Command Center ------------------------------------------------------------

_SCC_SEVERITY: dict[str, AlertSeverity] = {"CRITICAL": "CRITICAL", "HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW"}


def _gcp_key(credentials: dict[str, str]) -> dict:
    try:
        key = json.loads(credentials["credentials_json"])
    except (KeyError, ValueError, TypeError):
        raise SourceError("error", "GCP 키 형식이 서비스 계정 JSON 이 아닙니다") from None
    if not isinstance(key, dict) or not isinstance(key.get("project_id"), str):
        raise SourceError("error", "GCP 키에 project_id 가 없습니다")
    return key


def _gcp_token(key: dict) -> str:
    import google.auth.transport.urllib3
    import urllib3
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(
        key, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.urllib3.Request(urllib3.PoolManager()))
    return creds.token


def scc_alerts(credentials: dict[str, str], client: httpx.Client | None = None,
               token_fn: Callable[[dict], str] = _gcp_token) -> tuple[list[SecurityAlert], str]:
    key = _gcp_key(credentials)
    project = key["project_id"]
    try:
        token = token_fn(key)
    except Exception as exc:  # noqa: BLE001 - 서명 실패 원문에 키 정보가 섞일 수 있어 종류만 남깁니다
        raise SourceError("error", f"GCP 토큰 발급 실패 ({type(exc).__name__})") from None
    own = client is None
    client = client or httpx.Client(timeout=HTTP_TIMEOUT_S)
    try:
        response = client.get(
            f"{GCP_SCC}/projects/{quote(project, safe='')}/sources/-/findings",
            headers={"Authorization": f"Bearer {token}"},
            params={"filter": 'state="ACTIVE"', "pageSize": str(MAX_FINDINGS)},
        )
    except httpx.HTTPError as exc:
        raise SourceError("error", f"Security Command Center 연결 실패 ({type(exc).__name__})") from None
    finally:
        if own:
            client.close()
    if response.status_code in (400, 404) and "not enabled" in response.text.lower():
        raise SourceError("not_enabled", "Security Command Center 가 이 프로젝트에서 켜져 있지 않습니다")
    if not response.is_success:
        raise _http_status(response.status_code, "Security Command Center")
    try:
        body = response.json()
    except ValueError:
        raise SourceError("error", "Security Command Center 응답이 JSON 이 아닙니다") from None
    alerts = []
    for row in body.get("listFindingsResults", []) or []:
        f = row.get("finding") or {}
        resource = str(f.get("resourceName") or "unknown")
        alerts.append(SecurityAlert(
            id=f"gcp:{f.get('name', '')}",
            provider="gcp",
            severity=_SCC_SEVERITY.get(str(f.get("severity", "")).upper(), "LOW"),
            resource_type=_gcp_resource_type(resource),
            resource_id=resource,
            threat_category=str(f.get("category") or "unknown"),
            description=str(f.get("description") or f.get("category") or ""),
            detected_at=_parse_time(f.get("eventTime")),
        ))
    return alerts, project


def _gcp_resource_type(resource: str) -> ResourceType:
    if "sqladmin" in resource or ("/instances/" in resource and "sql" in resource):
        return "database"
    if "run.googleapis.com" in resource:
        return "container"
    if "compute.googleapis.com" in resource and "/instances/" in resource:
        return "vm"
    if "storage.googleapis.com" in resource:
        return "storage"
    if "iam.googleapis.com" in resource:
        return "iam_role"
    return "other"


# ---- Azure Defender for Cloud ---------------------------------------------------------------

_AZURE_SEVERITY: dict[str, AlertSeverity] = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW", "informational": "LOW"}


def defender_alerts(credentials: dict[str, str], client: httpx.Client | None = None) -> tuple[list[SecurityAlert], str]:
    for name in ("tenant_id", "client_id", "client_secret", "subscription_id"):
        if not credentials.get(name):
            raise SourceError("error", "Azure 키 형식이 맞지 않습니다")
    subscription = credentials["subscription_id"]
    own = client is None
    client = client or httpx.Client(timeout=HTTP_TIMEOUT_S)
    try:
        token_response = client.post(
            f"https://login.microsoftonline.com/{quote(credentials['tenant_id'], safe='')}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": credentials["client_id"],
                  "client_secret": credentials["client_secret"], "scope": f"{AZURE_ARM}/.default"},
        )
        if not token_response.is_success:
            raise _http_status(token_response.status_code, "Azure 로그인")
        token = token_response.json().get("access_token")
        response = client.get(
            f"{AZURE_ARM}/subscriptions/{quote(subscription, safe='')}/providers/Microsoft.Security/alerts",
            headers={"Authorization": f"Bearer {token}"},
            params={"api-version": AZURE_ALERTS_API_VERSION},
        )
    except httpx.HTTPError as exc:
        raise SourceError("error", f"Defender for Cloud 연결 실패 ({type(exc).__name__})") from None
    except ValueError:
        raise SourceError("error", "Azure 로그인 응답이 JSON 이 아닙니다") from None
    finally:
        if own:
            client.close()
    if not response.is_success:
        raise _http_status(response.status_code, "Defender for Cloud")
    try:
        rows = response.json().get("value", []) or []
    except ValueError:
        raise SourceError("error", "Defender for Cloud 응답이 JSON 이 아닙니다") from None
    alerts = []
    for row in rows:
        p = row.get("properties") or {}
        if str(p.get("status", "")).lower() != "active":
            continue
        ids = [r.get("azureResourceId") for r in p.get("resourceIdentifiers") or [] if isinstance(r, dict)]
        resource = next((i for i in ids if isinstance(i, str) and i), None) or str(p.get("compromisedEntity") or "unknown")
        alerts.append(SecurityAlert(
            id=f"azure:{row.get('name', '')}",
            provider="azure",
            severity=_AZURE_SEVERITY.get(str(p.get("severity", "")).lower(), "LOW"),
            resource_type=_azure_resource_type(resource),
            resource_id=resource,
            threat_category=str(p.get("alertType") or "unknown"),
            description=str(p.get("alertDisplayName") or p.get("description") or ""),
            detected_at=_parse_time(p.get("timeGeneratedUtc")),
        ))
    return alerts, subscription


def _azure_resource_type(resource: str) -> ResourceType:
    lowered = resource.lower()
    if "microsoft.dbforpostgresql" in lowered or "microsoft.sql" in lowered:
        return "database"
    if "microsoft.app/" in lowered or "microsoft.containerinstance" in lowered:
        return "container"
    if "microsoft.compute/virtualmachines" in lowered:
        return "vm"
    if "microsoft.storage" in lowered:
        return "storage"
    return "other"


# ---- 묶음 + 캐시 ------------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, list[SecurityAlert], SourceStatus]] = {}
_clock = time.monotonic


def _status(provider: str, service: str, status: str, scope: str | None,
            message: str | None = None, count: int | None = None) -> SourceStatus:
    return SourceStatus(provider=provider, service=service, status=status, scope=scope,
                        count=count, checked_at=_now(), message=message)


def _cached(key: tuple, fetch: Callable[[], tuple[list[SecurityAlert], SourceStatus]]) -> tuple[list[SecurityAlert], SourceStatus]:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and _clock() - hit[0] < CACHE_TTL_S:
            return hit[1], hit[2]
    alerts, status = fetch()
    with _cache_lock:
        _cache[key] = (_clock(), alerts, status)
    return alerts, status


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _aws(regions: list[str]) -> tuple[list[SecurityAlert], list[SourceStatus]]:
    if not regions:
        return [], [_status("aws", "GuardDuty", "no_scope", None, "run 에 AWS 리전이 없어 확인할 리전을 모릅니다")]
    alerts: list[SecurityAlert] = []
    statuses: list[SourceStatus] = []
    for region in regions:
        def fetch(region: str = region) -> tuple[list[SecurityAlert], SourceStatus]:
            try:
                found = guardduty_alerts(region, _credentials("aws"))
            except SourceError as exc:
                return [], _status("aws", "GuardDuty", exc.status, region, exc.message)
            return found, _status("aws", "GuardDuty", "ok", region, count=len(found))
        found, status = _cached(("aws", region), fetch)
        alerts += found
        statuses.append(status)
    return alerts, statuses


def _gcp() -> tuple[list[SecurityAlert], SourceStatus]:
    def fetch() -> tuple[list[SecurityAlert], SourceStatus]:
        try:
            found, project = scc_alerts(_credentials("gcp"))
        except SourceError as exc:
            return [], _status("gcp", "Security Command Center", exc.status, None, exc.message)
        return found, _status("gcp", "Security Command Center", "ok", project, count=len(found))
    return _cached(("gcp",), fetch)


def _azure() -> tuple[list[SecurityAlert], SourceStatus]:
    def fetch() -> tuple[list[SecurityAlert], SourceStatus]:
        try:
            found, subscription = defender_alerts(_credentials("azure"))
        except SourceError as exc:
            return [], _status("azure", "Defender for Cloud", exc.status, None, exc.message)
        return found, _status("azure", "Defender for Cloud", "ok", subscription, count=len(found))
    return _cached(("azure",), fetch)


def fetch_all(aws_regions: list[str]) -> tuple[list[SecurityAlert], list[SourceStatus]]:
    """세 클라우드 경보와 각 확인 결과. 한 곳이 실패해도 나머지는 돌려줍니다."""
    alerts, statuses = _aws(aws_regions)
    for found, status in (_gcp(), _azure()):
        alerts += found
        statuses.append(status)
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    alerts.sort(key=lambda a: order[a.severity])
    return alerts, statuses
