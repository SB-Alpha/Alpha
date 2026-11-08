"""기존 원격 관리 터널의 명시한 호스트만 연결한다. Access 정책을 자동 활성화하지 않는다."""

from copy import deepcopy
import time

import httpx

from app.features.provisioning.terraform import ProvisioningError


class Cloudflare:
    def __init__(self, config, token: str, client=None):
        self.config = config
        self.client = client or httpx.Client(
            base_url="https://api.cloudflare.com/client/v4",
            timeout=20,
            headers={"Authorization": "Bearer " + token},
            follow_redirects=False,
        )

    def call(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
            response.raise_for_status()
            data = response.json()
            if not data.get("success"):
                raise ValueError()
            return data["result"]
        except (httpx.HTTPError, ValueError, KeyError):
            # Cloudflare 원문·토큰·응답 본문을 로그에 싣지 않는다.
            raise ProvisioningError(
                "Cloudflare API 요청 실패 (권한·설정 확인 필요)"
            ) from None

    @property
    def tunnel_path(self):
        c = self.config
        return f"/accounts/{c.account_id}/cfd_tunnel/{c.tunnel_id}"

    def check_db_access(self):
        c = self.config
        path = f"/accounts/{c.account_id}/access/apps/{c.db_access_app_id}"
        app = self.call("GET", path)
        policies = self.call("GET", path + "/policies")
        if app.get("domain") != c.db_hostname:
            raise ProvisioningError(
                "DB Access 앱의 domain이 목적지 DB 호스트명과 다릅니다"
            )
        service = [p for p in policies if p.get("decision") == "non_identity"]
        if (
            not service
            or any(p.get("decision") not in {"non_identity", "deny"} for p in policies)
            or any(
                not p.get("include")
                or any("service_token" not in i for i in p["include"])
                for p in service
            )
        ):
            raise ProvisioningError(
                "DB Access에는 서비스 토큰 전용 Service Auth 정책이 먼저 필요합니다"
            )

    def ensure_route(self, hostname: str, service: str):
        c = self.config
        zone = self.call("GET", f"/zones/{c.zone_id}")
        zone_name = zone.get("name", "")
        if not zone_name or not (
            hostname == zone_name or hostname.endswith("." + zone_name)
        ):
            raise ProvisioningError(
                "호스트명이 지정한 Cloudflare DNS zone에 속하지 않습니다"
            )
        existing = self.call("GET", self.tunnel_path + "/configurations")
        config = deepcopy(existing.get("config") or {})
        ingress = config.get("ingress") or [{"service": "http_status:404"}]
        matches = [r for r in ingress if r.get("hostname") == hostname]
        if matches and (
            len(matches) != 1
            or matches[0].get("service") != service
            or matches[0].get("path")
        ):
            raise ProvisioningError(
                "동일 호스트명의 다른 터널 경로가 있습니다. 자동 덮어쓰기를 거부합니다"
            )
        records = self.call(
            "GET", f"/zones/{c.zone_id}/dns_records", params={"name": hostname}
        )
        content = c.tunnel_id + ".cfargotunnel.com"
        if records and (
            len(records) != 1
            or records[0].get("type") != "CNAME"
            or records[0].get("content") != content
            or not records[0].get("proxied")
        ):
            raise ProvisioningError("기존 DNS 레코드가 다른 목적지를 가리킵니다")
        if not matches:
            if ingress[-1].get("hostname") or ingress[-1].get("path"):
                raise ProvisioningError("터널의 마지막 catch-all 경로가 없습니다")
            # 와일드카드보다 앞에서 정확한 호스트를 매칭하며 다른 경로는 보존한다.
            config["ingress"] = [{"hostname": hostname, "service": service}, *ingress]
            self.call(
                "PUT", self.tunnel_path + "/configurations", json={"config": config}
            )
        if not records:
            self.call(
                "POST",
                f"/zones/{c.zone_id}/dns_records",
                json={
                    "type": "CNAME",
                    "name": hostname,
                    "content": content,
                    "proxied": True,
                },
            )

    def healthy(self):
        deadline = time.monotonic() + getattr(self.config, "health_timeout_s", 120)
        while True:
            if self.call("GET", self.tunnel_path).get("status") == "healthy":
                return
            if time.monotonic() >= deadline:
                raise ProvisioningError("Cloudflare 터널 준비 확인 시간 초과")
            time.sleep(2)
