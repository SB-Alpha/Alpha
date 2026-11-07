"""Removes secret values from error text that leaves the agent."""

import re
from collections.abc import Iterable

MASK = "[hidden]"
# Shorter values would mask ordinary words in the text.
MIN_VALUE_LENGTH = 4
_PATTERNS = (
    re.compile(r"(://[^\s:/@]+:)[^\s@/]+(@)"),
    re.compile(r"(?i)\b((?:password|passwd|pwd|secret|token)[\"']?\s*[=:]\s*)(?!//)[^\s,;&\"']+"),
    re.compile(r"(?i)(bearer\s+)\S+"),
)


def _mask_match(match: re.Match) -> str:
    groups = [g for g in match.groups() if g]
    return groups[0] + MASK + (groups[1] if len(groups) > 1 else "")


def clean(text: str, values: Iterable[str] = ()) -> str:
    # Longest first, so a value that contains another one goes as a whole.
    for value in sorted({v for v in values if len(v) >= MIN_VALUE_LENGTH}, key=len, reverse=True):
        text = text.replace(value, MASK)
    for pattern in _PATTERNS:
        text = pattern.sub(_mask_match, text)
    return text
