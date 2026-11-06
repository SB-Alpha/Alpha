"""Access rule for local APIs that handle migrations and secrets.

The backend runs on the user's PC, where any local process or web page could reach its port.
This is not a user login: the app holds one token per launch, and only it can call these routes.
"""

import ipaddress
import logging
import os
import secrets
import threading
from pathlib import Path

from fastapi import HTTPException, Request

from app.core.config import data_dir

logger = logging.getLogger(__name__)

TOKEN_FILE = "api-token"
DEFAULT_HOSTS = ("localhost", "127.0.0.1", "::1")

_token: str | None = None
_lock = threading.Lock()


def _write_token(path: Path, token: str) -> None:
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)


def ensure_api_token() -> str:
    global _token
    configured = os.getenv("ALPHA_API_TOKEN")
    if configured:
        return configured
    with _lock:
        if _token is None:
            _token = secrets.token_urlsafe(32)
            path = data_dir() / TOKEN_FILE
            _write_token(path, _token)
            logger.info("local API token written to %s", path)
        return _token


def _allowed_hosts() -> set[str]:
    configured = os.getenv("ALPHA_ALLOWED_HOSTS")
    names = configured.split(",") if configured else DEFAULT_HOSTS
    return {name.strip().lower() for name in names if name.strip()}


def _host_name(header: str) -> str:
    header = header.strip().lower()
    if header.startswith("["):  # [::1]:8000
        return header[1:].split("]", 1)[0]
    return header.split(":", 1)[0]


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_local_access(request: Request) -> None:
    if not request.client or not _is_loopback(request.client.host):
        raise HTTPException(403, "only this PC can call this API")
    # A web page can resolve its own name to 127.0.0.1; the Host header still shows that name.
    if _host_name(request.headers.get("host", "")) not in _allowed_hosts():
        raise HTTPException(403, "this Host is not allowed")
    scheme, _, given = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(given.encode(), ensure_api_token().encode()):
        raise HTTPException(401, "a valid API token is required", headers={"WWW-Authenticate": "Bearer"})
