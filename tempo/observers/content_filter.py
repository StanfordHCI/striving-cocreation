"""Lightweight regex-based PII / sensitive content detection.

Checks OCR-extracted text for credit card numbers, SSNs, API keys,
and password fields. No ML dependencies required.
"""
from __future__ import annotations

import re
from typing import Optional, TypedDict


class SensitiveMatch(TypedDict):
    reason: str
    matched: str


def _luhn_check(digits: str) -> bool:
    """Validate a digit string using the Luhn algorithm."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# Pre-compiled patterns
_CC_PATTERN = re.compile(r"\b(\d[ -]?){13,19}\b")
_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_AWS_KEY_PATTERN = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_API_KEY_PATTERN = re.compile(r"\b(sk-[a-zA-Z0-9]{20,}|key-[a-zA-Z0-9]{20,})\b")
_SECRET_LABEL_PATTERN = re.compile(
    r"(?:password|passwd|secret|api[_\s]?key|access[_\s]?token)\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def check_sensitive(text: str) -> Optional[SensitiveMatch]:
    """Check text for sensitive/PII content.

    Returns None if clean, or a dict with reason and matched snippet.
    """
    # Credit card numbers (13-19 digits, Luhn validated)
    for m in _CC_PATTERN.finditer(text):
        digits = re.sub(r"[- ]", "", m.group())
        if 13 <= len(digits) <= 19 and digits.isdigit() and _luhn_check(digits):
            return {"reason": "credit_card", "matched": m.group().strip()}

    # SSNs
    m = _SSN_PATTERN.search(text)
    if m:
        return {"reason": "ssn", "matched": m.group()}

    # AWS access keys
    m = _AWS_KEY_PATTERN.search(text)
    if m:
        return {"reason": "aws_key", "matched": m.group()}

    # API keys (sk-*, key-*)
    m = _API_KEY_PATTERN.search(text)
    if m:
        return {"reason": "api_key", "matched": m.group()}

    # Password / secret labels with values
    m = _SECRET_LABEL_PATTERN.search(text)
    if m:
        return {"reason": "password_or_secret", "matched": m.group()}

    return None
