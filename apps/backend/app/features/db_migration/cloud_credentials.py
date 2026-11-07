"""Where the screen registers the cloud keys every run on this app shares.

Values go in once and never come back: responses say only whether a cloud is ready.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.core.secrets import delete_secret, put_secret

from .cloud_lookup import LookupFailed, check_credentials
from .run_input import CLOUD_CREDENTIAL_KEYS, Cloud, credentials_registered
from .schemas import CloudStatus, single_line

router = APIRouter(prefix="/clouds")


async def _body(request: Request) -> Any:
    # Read by hand: FastAPI's own validation errors quote the submitted value back.
    try:
        return await request.json()
    except ValueError:
        raise HTTPException(422, "body must be a JSON object") from None


def _status(cloud: str) -> CloudStatus:
    return CloudStatus(cloud=cloud, registered=credentials_registered(cloud),
                       required_keys=sorted(CLOUD_CREDENTIAL_KEYS[cloud]))


def _check(cloud: str, value: Any) -> dict[str, str]:
    expected = CLOUD_CREDENTIAL_KEYS[cloud]
    # Key names only in the message; a wrong key could be a pasted value.
    if not isinstance(value, dict) or value.keys() != expected:
        raise HTTPException(422, f"wrong keys for {cloud}; required: {', '.join(sorted(expected))}")
    if not all(isinstance(v, str) and v for v in value.values()):
        raise HTTPException(422, f"values for {cloud} must be non-empty strings")
    # A service account key is JSON and may span lines. The others are single values.
    if cloud != "gcp" and not all(single_line(v) for v in value.values()):
        raise HTTPException(422, f"values for {cloud} must be single lines without surrounding spaces")
    try:
        check_credentials(cloud, value)
    except LookupFailed as exc:
        raise HTTPException(422, str(exc)) from None
    return value


@router.get("")
def list_clouds() -> list[CloudStatus]:
    """Which clouds have keys, and the keys a registration form needs. Never includes the values."""
    return [_status(cloud) for cloud in CLOUD_CREDENTIAL_KEYS]


@router.put("/{cloud}")
def register(cloud: Cloud, value: Annotated[Any, Depends(_body)]) -> CloudStatus:
    """Encrypt and store the keys of one cloud. The same cloud overwrites."""
    put_secret(f"cloud/{cloud}", _check(cloud, value))
    return _status(cloud)


@router.delete("/{cloud}", status_code=204)
def remove(cloud: Cloud) -> Response:
    """Remove only when no run still has a VPN on this cloud: removing the VPN needs these keys."""
    delete_secret(f"secret://cloud/{cloud}")
    return Response(status_code=204)
