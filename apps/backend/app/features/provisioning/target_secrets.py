# terraform 의 민감 출력을 비밀 저장소에 넣는다. db_migration 이 이름표(secret://<run_id>/...)를 resolve() 로 꺼내 쓴다.
# 값은 저장소로만 가고, 결과·진행 메시지·오류 메시지에는 넣지 않는다 (이름만 남긴다).
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .target import default_credentials_ref, unwrap_outputs
from .terraform import ProvisioningError

PutSecret = Callable[[str, dict[str, str]], str]


def _default_put(name: str, value: dict[str, str]) -> str:
    from app.core.secrets import put_secret  # 암호화 저장소 (PR #29)

    return put_secret(name, value)


def _save(put: PutSecret, ref: str, value: dict[str, str]) -> None:
    try:
        put(ref, value)
    except Exception as error:  # 저장소·암호화 키 문제. 종류만 남기고 값은 남기지 않는다.
        raise ProvisioningError(f"비밀 저장소에 {ref} 를 저장하지 못했습니다 ({type(error).__name__})") from None


def store_target_secrets(
    outputs: Mapping[str, Any], *, run_id: str, put: PutSecret | None = None
) -> tuple[list[str], list[str]]:
    """목적지 관리자 계정(target-admin)과 VPN 용 자격 증명(target-cloud)을 이 run 의 이름표로 저장한다.

    이름표는 build_target 의 기본 이름표와 같은 함수에서 가져온다. 같은 이름이 있으면 덮어쓴다.
    돌려주는 값: (저장한 이름표 목록, 저장하지 못한 것에 대한 경고 목록)
    """
    values = unwrap_outputs(outputs)
    refs = default_credentials_ref(run_id)
    save = put or _default_put

    # 관리자 계정이 없으면 db_migration 이 시작할 수 없다. 하나도 저장하기 전에 멈춘다.
    admin = {"user": values.get("db_admin_user"), "password": values.get("db_admin_password")}
    missing = [f"db_admin_{key}" for key, value in admin.items() if not value]
    if missing:
        raise ProvisioningError("terraform 출력에 없는 값: " + ", ".join(missing))
    _save(save, refs["admin"], {key: str(value) for key, value in admin.items()})
    stored = [refs["admin"]]

    warnings: list[str] = []
    vpn_key = values.get("vpn_credentials_json")
    if vpn_key:
        _save(save, refs["cloud"], {"credentials_json": str(vpn_key)})
        stored.append(refs["cloud"])
    else:
        warnings.append("vpn_credentials_json 출력이 없어 target-cloud 를 저장하지 못했습니다 (VPN 을 만들려면 필요합니다)")
    return stored, warnings
