from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any

# 목적지 대역이 겹치면 안 되는 상대 쪽 대역
PEER_CIDRS = {"OCI": "10.50.0.0/16", "AWS": "172.31.0.0/16"}

# target 을 만드는 데 꼭 필요한 terraform 출력 이름
REQUIRED_OUTPUTS = (
    "vpc_name",
    "subnet_cidrs",
    "psa_range_cidr",
    "db_instance",
    "db_private_ip",
    "db_port",
    "db_name",
    "db_version",
    "db_ssl_mode",
    "db_user",
)


class TargetBuildError(ValueError):
    """terraform 출력으로 target 을 만들 수 없을 때."""


def unwrap_outputs(raw: Mapping[str, Any]) -> dict[str, Any]:
    """`terraform output -json` 의 {"이름": {"value": ..., "sensitive": ...}} 를 {"이름": 값} 으로 푼다.

    이미 값만 담은 dict 이면 그대로 돌려준다.
    """
    values: dict[str, Any] = {}
    for name, item in raw.items():
        values[name] = item["value"] if isinstance(item, Mapping) and "value" in item else item
    return values


def default_credentials_ref(run_id: str) -> dict[str, str]:
    """이 run 에서 쓸 비밀 이름표. 호출하는 쪽이 같은 이름으로 값을 저장해야 한다."""
    return {
        "cloud": f"secret://{run_id}/target-cloud",  # VPN 용 GCP 자격 증명
        "admin": f"secret://{run_id}/target-admin",  # {"user": "postgres", "password": ...}
    }


def build_target(
    outputs: Mapping[str, Any],
    *,
    run_id: str,
    project_id: str,
    region: str,
    credentials_ref: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """terraform 출력을 안재현님 명세 2절 모양의 target 으로 바꾼다.

    credentials_ref 를 주지 않으면 기본 이름표를 쓴다. put_secret 이 돌려준 이름표가 있으면 그것을 넘긴다.
    app.url 은 앱을 배포하기 전(deploy_app=false)에는 None 이다.
    """
    values = unwrap_outputs(outputs)
    missing = [name for name in REQUIRED_OUTPUTS if values.get(name) in (None, "", [])]
    if missing:
        raise TargetBuildError("terraform 출력에 없는 값: " + ", ".join(missing))

    return {
        "project_id": project_id,
        "region": region,
        "network": {
            "vpc_name": values["vpc_name"],
            "subnet_cidrs": list(values["subnet_cidrs"]),
            "psa_range_cidr": values["psa_range_cidr"],
        },
        "db": {
            "instance": values["db_instance"],
            "private_ip": values["db_private_ip"],
            "port": int(values["db_port"]),
            "database": values["db_name"],
            "version": values["db_version"],
            "ssl_mode": values["db_ssl_mode"],
        },
        "app": {"url": values.get("app_url"), "db_user": values["db_user"]},
        "credentials_ref": dict(credentials_ref) if credentials_ref else default_credentials_ref(run_id),
    }


def find_problems(target: Mapping[str, Any]) -> list[str]:
    """CDC 가 목적지에 닿지 못하게 만드는 문제를 찾는다.

    문제가 없으면 빈 목록. 막을지 경고만 할지는 호출하는 쪽이 정한다.
    """
    problems: list[str] = []
    db = target["db"]

    if db["ssl_mode"] != "ENCRYPTED_ONLY":
        problems.append(f"db.ssl_mode 가 ENCRYPTED_ONLY 여야 합니다 (현재 {db['ssl_mode']})")

    version = str(db["version"]).removeprefix("POSTGRES_")
    if not version.isdigit() or int(version) < 16:
        problems.append(f"PostgreSQL 16 이상이어야 합니다 (현재 {db['version']})")

    network = target["network"]
    ours = {"psa_range_cidr": [network["psa_range_cidr"]], "subnet_cidrs": network["subnet_cidrs"]}
    for field, cidrs in ours.items():
        for cidr in cidrs:
            for peer, peer_cidr in PEER_CIDRS.items():
                if ipaddress.ip_network(cidr, strict=False).overlaps(ipaddress.ip_network(peer_cidr)):
                    problems.append(f"network.{field} {cidr} 가 {peer} 대역 {peer_cidr} 와 겹칩니다")
    return problems
