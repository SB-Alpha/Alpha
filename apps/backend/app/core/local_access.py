"""Access rule for local APIs that handle migrations and secrets.

The backend runs on the user's PC, where any web page in a browser could send it requests.
This rule refuses other machines and other sites' pages. It is not a user login, and a program
running on this PC can still call these routes: such a program already has the user's rights.
"""

import ipaddress
import os

from fastapi import HTTPException, Request

DEFAULT_HOSTS = ("localhost", "127.0.0.1", "::1")
DEFAULT_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000")


def _configured(name: str, default: tuple[str, ...]) -> set[str]:
    configured = os.getenv(name)
    values = configured.split(",") if configured else default
    return {value.strip().lower() for value in values if value.strip()}


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
    if _host_name(request.headers.get("host", "")) not in _configured("ALPHA_ALLOWED_HOSTS", DEFAULT_HOSTS):
        raise HTTPException(403, "this Host is not allowed")
    # Browsers set Origin on requests that change data, and a page cannot change it. Programs send none.
    origin = request.headers.get("origin")
    if origin is not None and origin.strip().lower() not in _configured("ALPHA_ALLOWED_ORIGINS", DEFAULT_ORIGINS):
        raise HTTPException(403, "this Origin is not allowed")
