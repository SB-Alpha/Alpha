# terraform 의 민감 출력을 비밀 저장소에 넣는다. db_migration 이 이름표(secret://<run_id>/...)를 resolve() 로 꺼내 쓴다.
# 값은 저장소로만 가고, 결과·진행 메시지·오류 메시지에는 넣지 않는다 (이름만 남긴다).
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from .target import default_credentials_ref, unwrap_outputs
from .terraform import ProvisioningError

PutSecret = Callable[[str, dict[str, str]], str]
SecretExists = Callable[[str], bool]

# 비밀 입력 API(db_migration)의 run_id 규칙과 같다. 이름표는 "secret://<run_id>/<tag>" 라서 "/" 가 들어가면
# 다른 이름표와 겹칠 수 있다. 모듈끼리 직접 부르지 않으려고 규칙을 여기에도 둔다.
_RUN_ID = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


def _default_put(name: str, value: dict[str, str]) -> str:
    from app.core.secrets import put_secret  # 암호화 저장소 (PR #29)

    return put_secret(name, value)


def _default_exists(name: str) -> bool:
    from app.core.secrets import has_secret

    return has_secret(name)


def _save(put: PutSecret, ref: str, value: dict[str, str]) -> None:
    try:
        put(ref, value)
    except Exception as error:  # 저장소·암호화 키 문제. 종류만 남기고 값은 남기지 않는다.
        raise ProvisioningError(f"비밀 저장소에 {ref} 를 저장하지 못했습니다 ({type(error).__name__})") from None


def store_target_secrets(
    outputs: Mapping[str, Any], *, run_id: str, put: PutSecret | None = None, exists: SecretExists | None = None
) -> tuple[list[str], list[str]]:
    """목적지 관리자 계정(target-admin)과 VPN 용 자격 증명(target-cloud)을 이 run 의 이름표로 저장한다.

    이름표는 build_target 의 기본 이름표와 같은 함수에서 가져온다.
    덮어쓰기 규칙 (의도한 동작):
    - target-admin: 화면에서 미리 넣은 값이 있어도 Terraform 이 만든 값으로 덮어쓴다. 목적지 DB 의 관리자 비밀번호는
      Terraform 이 새로 만든 것이라, 사용자가 넣은 값은 이 DB 에 맞지 않는다.
    - target-cloud: Terraform 출력에 키가 있을 때만 덮어쓴다. 없으면(create_vpn_key=false) 사용자가 넣은 값을 그대로 둔다.
    돌려주는 값: (저장한 이름표 목록, 저장하지 못한 것에 대한 경고 목록)
    """
    if not _RUN_ID.fullmatch(run_id):
        raise ProvisioningError("run_id 형식이 올바르지 않아 비밀을 저장하지 않았습니다 (소문자로 시작하는 소문자·숫자·하이픈 2~31자)")
    values = unwrap_outputs(outputs)
    refs = default_credentials_ref(run_id)
    save = put or _default_put
    has = exists or _default_exists

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
    elif not has(refs["cloud"]):  # 사용자가 화면에서 직접 넣어 둔 경우에는 경고하지 않는다
        warnings.append("vpn_credentials_json 출력이 없어 target-cloud 를 저장하지 못했습니다 (VPN 을 만들려면 필요합니다)")
    return stored, warnings
