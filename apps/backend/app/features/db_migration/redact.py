"""Removes secret values from error text before it leaves the module."""

import re
from collections.abc import Iterable

from app.core.secrets import has_secret, resolve

MASK = "[hidden]"
# Shorter values would mask ordinary words in the text.
MIN_VALUE_LENGTH = 4
# Account names are not secret and help to read an error.
_NOT_SECRET_KEYS = {"user"}
_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"(://[^\s:/@]+:)[^\s@/]+(@)"),
    re.compile(r"(?i)\b((?:password|passwd|pwd|secret|token|private_key)[\"']?\s*[=:]\s*)(?!//)[^\s,;&\"']+"),
    re.compile(r"(?i)(bearer\s+)\S+"),
)


def _mask_match(match: re.Match) -> str:
    groups = [g for g in match.groups() if g and not g.startswith("-----END")]
    if not groups:
        return MASK
    return groups[0] + MASK + (groups[1] if len(groups) > 1 else "")


def clean(text: str, values: Iterable[str] = ()) -> str:
    # Longest first, so a value that contains another one goes as a whole.
    for value in sorted({v for v in values if len(v) >= MIN_VALUE_LENGTH}, key=len, reverse=True):
        text = text.replace(value, MASK)
    for pattern in _PATTERNS:
        text = pattern.sub(_mask_match, text)
    return text


def run_values(run_id: str, tags: Iterable[str]) -> list[str]:
    """The secret values saved under a run. Used only to find them in text."""
    return ref_values(f"secret://{run_id}/{tag}" for tag in tags)


def ref_values(refs: Iterable[str]) -> list[str]:
    values: list[str] = []
    for ref in refs:
        try:
            if has_secret(ref):
                values += [v for k, v in resolve(ref).items() if k not in _NOT_SECRET_KEYS]
        except Exception:  # noqa: BLE001 - the secret store must not hide the error being cleaned
            continue
    return values
