"""Deterministic scrubbing before any text leaves the platform.

We sanitise *before* the model call instead of asking the model to ignore
secrets, and then run a second, independent verification pass. If anything
survives verification the caller must not send the text, and must emit the
``sanitiser_bypass`` event (which pages).

Note: payment-service logs can contain primary account numbers (PANs).
Card-like digit runs are masked only when they pass the Luhn check, so order
IDs and timestamps are left alone.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

_REDACTED = "[REDACTED:{}]"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("gcp_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("aws_access_key", re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+(?!\[REDACTED)[A-Za-z0-9._~+/=-]{16,}")),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|token)\b(\s*[:=]\s*)"
            r"(?!\[REDACTED)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
        ),
    ),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
]

_PAN_CANDIDATE = re.compile(r"(?<![\d.])(?:\d[ -]?){12,18}\d(?![\d.])")


def luhn_valid(digits: str) -> bool:
    if not 13 <= len(digits) <= 19 or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _is_pan(candidate: str) -> bool:
    return luhn_valid(re.sub(r"[ -]", "", candidate))


@dataclass
class SanitiseResult:
    text: str
    hits: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.hits.values())


def sanitise(text: str) -> SanitiseResult:
    hits: Counter[str] = Counter()
    out = text
    for name, pattern in _PATTERNS:
        if name == "secret_assignment":

            def _keep_key(m: re.Match[str]) -> str:
                return f"{m.group(1)}{m.group(2)}{_REDACTED.format('secret')}"

            out, n = pattern.subn(_keep_key, out)
        else:
            out, n = pattern.subn(_REDACTED.format(name), out)
        if n:
            hits[name] += n

    def _mask_pan(m: re.Match[str]) -> str:
        if _is_pan(m.group(0)):
            hits["pan"] += 1
            return _REDACTED.format("pan")
        return m.group(0)

    out = _PAN_CANDIDATE.sub(_mask_pan, out)
    return SanitiseResult(out, hits)


def verify(text: str) -> list[str]:
    """Independent second pass. Returns the names of any patterns that still match."""
    survivors = [name for name, pattern in _PATTERNS if pattern.search(text)]
    if any(_is_pan(m.group(0)) for m in _PAN_CANDIDATE.finditer(text)):
        survivors.append("pan")
    return survivors
