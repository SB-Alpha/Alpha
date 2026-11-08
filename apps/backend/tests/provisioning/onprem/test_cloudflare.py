import json
from types import SimpleNamespace
import httpx
import pytest
from app.features.provisioning.onprem.cloudflare import Cloudflare
from app.features.provisioning.terraform import ProvisioningError


def make(ingress=None, records=None, policies=None):
    calls = []
    cfg = SimpleNamespace(
        account_id="account",
        zone_id="zone",
        tunnel_id="tunnel",
        db_access_app_id="access",
        db_hostname="db.example.com",
    )

    def handle(req):
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body))
        path = req.url.path
        if path.endswith("/zones/zone"):
            result = {"name": "example.com"}
        elif path.endswith("/policies"):
            result = (
                policies
                if policies is not None
                else [
                    {
                        "decision": "non_identity",
                        "include": [{"service_token": {"token_id": "id"}}],
                    }
                ]
            )
        elif path.endswith("/access/apps/access"):
            result = {"domain": "db.example.com"}
        elif path.endswith("/configurations"):
            result = {
                "config": {
                    "originRequest": {"connectTimeout": 10},
                    "ingress": ingress
                    or [
                        {"hostname": "old.example.com", "service": "http://old:80"},
                        {"service": "http_status:404"},
                    ],
                }
            }
        elif path.endswith("/dns_records"):
            result = [] if records is None else records
        else:
            result = {"status": "healthy"}
        return httpx.Response(200, json={"success": True, "result": result})

    client = httpx.Client(
        base_url="https://api.cloudflare.com/client/v4",
        transport=httpx.MockTransport(handle),
    )
    return Cloudflare(cfg, "secret-sentinel", client), calls


def test_route_preserves_unrelated_ingress_and_origin_settings():
    edge, calls = make()
    edge.ensure_route("app.example.com", "http://was:8080")
    put = next(body for method, _, body in calls if method == "PUT")
    assert put["config"]["originRequest"] == {"connectTimeout": 10}
    assert put["config"]["ingress"] == [
        {"hostname": "app.example.com", "service": "http://was:8080"},
        {"hostname": "old.example.com", "service": "http://old:80"},
        {"service": "http_status:404"},
    ]
    assert (
        next(body for method, _, body in calls if method == "POST")["proxied"] is True
    )


def test_same_route_is_idempotent():
    edge, calls = make(
        ingress=[
            {"hostname": "app.example.com", "service": "http://was:8080"},
            {"service": "http_status:404"},
        ],
        records=[
            {"type": "CNAME", "content": "tunnel.cfargotunnel.com", "proxied": True}
        ],
    )
    edge.ensure_route("app.example.com", "http://was:8080")
    assert all(method == "GET" for method, _, _ in calls)


@pytest.mark.parametrize(
    "records",
    [
        [{"type": "A", "content": "1.2.3.4"}],
        [{"type": "CNAME", "content": "other.cfargotunnel.com", "proxied": True}],
    ],
)
def test_conflicting_dns_is_never_overwritten(records):
    edge, calls = make(records=records)
    with pytest.raises(ProvisioningError):
        edge.ensure_route("app.example.com", "http://was:8080")
    assert all(m == "GET" for m, _, _ in calls)


def test_other_route_cannot_be_hijacked():
    edge, calls = make(
        ingress=[
            {"hostname": "app.example.com", "service": "http://other:80"},
            {"service": "http_status:404"},
        ]
    )
    with pytest.raises(ProvisioningError):
        edge.ensure_route("app.example.com", "http://was:8080")
    assert all(m == "GET" for m, _, _ in calls)


@pytest.mark.parametrize(
    "policies",
    [
        [],
        [{"decision": "bypass"}],
        [{"decision": "allow"}],
        [{"decision": "non_identity", "include": [{"everyone": {}}]}],
    ],
)
def test_database_requires_existing_service_token_only_access(policies):
    edge, _ = make(policies=policies)
    with pytest.raises(ProvisioningError):
        edge.check_db_access()


def test_database_access_service_token_policy_is_accepted():
    edge, _ = make()
    edge.check_db_access()
