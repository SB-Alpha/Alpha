"""Where the screen and other modules save the secret values a run needs.

Values go in once and come back only as references; no route returns one. Secrets this module
makes itself (source-repl, vpn-psk, vpn-context) cannot be touched from here.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from app.core.secrets import PREFIX, delete_secret, has_secret, put_secret

# tag -> (required keys, optional keys)
INPUT_SECRETS: dict[str, tuple[set[str], set[str]]] = {
    "source-cloud": ({"aws_access_key_id", "aws_secret_access_key"}, {"aws_session_token"}),
    "source-db-admin": ({"user", "password"}, set()),
    "target-cloud": ({"credentials_json"}, set()),
    "target-admin": ({"user", "password"}, set()),
    "target-ddl-user": ({"user", "password"}, set()),
    "target-sink-user": ({"user", "password"}, set()),
}

router = APIRouter(prefix="/secrets")

RunId = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{1,30}$")]


def _tag(tag: str) -> str:
    if tag not in INPUT_SECRETS:
        raise HTTPException(404, f"unknown secret, expected one of: {', '.join(sorted(INPUT_SECRETS))}")
    return tag


Tag = Annotated[str, Depends(_tag)]


async def _body(request: Request) -> Any:
    # Read by hand: FastAPI's own validation errors quote the submitted value back.
    try:
        return await request.json()
    except ValueError:
        raise HTTPException(422, "body must be a JSON object") from None


def _ref(run_id: str, tag: str) -> str:
    return f"{PREFIX}{run_id}/{tag}"


def _check(tag: str, value: Any) -> dict[str, str]:
    required, optional = INPUT_SECRETS[tag]
    expected = f"required: {', '.join(sorted(required))}"
    if optional:
        expected += f"; optional: {', '.join(sorted(optional))}"
    # Key names only in the message; a wrong key could be a pasted value.
    if not isinstance(value, dict) or not required <= value.keys() <= required | optional:
        raise HTTPException(422, f"wrong keys for {tag} ({expected})")
    if not all(isinstance(v, str) and v for v in value.values()):
        raise HTTPException(422, f"values for {tag} must be non-empty strings")
    return value


@router.put("/{run_id}/{tag}")
def save(run_id: RunId, tag: Tag, value: Annotated[Any, Depends(_body)]) -> dict:
    """Encrypt and store one value (the same name overwrites). Returns the reference for run."""
    return {"ref": put_secret(_ref(run_id, tag), _check(tag, value))}


@router.get("/{run_id}")
def saved(run_id: RunId) -> dict:
    """Which inputs are saved for this run. Never includes the values."""
    return {tag: {"ref": _ref(run_id, tag), "saved": has_secret(_ref(run_id, tag))} for tag in INPUT_SECRETS}


@router.delete("/{run_id}/{tag}", status_code=204)
def remove(run_id: RunId, tag: Tag) -> Response:
    """Remove the cloud keys only after stop: removing the VPN needs them."""
    delete_secret(_ref(run_id, tag))
    return Response(status_code=204)
