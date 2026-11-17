"""Value-level masking of personal data in source tool results, the second layer after the MCP
server's column masking (`mcp/mysql_source/masking.py`).

The server masks columns whose name, comment or table says they hold personal data. This pass
catches what a column's name doesn't announce: an email address in a free-text note, a phone
number in a comment, a card number in a reference field. It runs on every string in a result
before the result is stored as evidence or shown to an agent, and replaces each match with the
same kind of marker the server uses. Numbers and dates are left alone; only text can carry these.
"""

import re
from typing import Any

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
# US-style SSNs, written with their separators (bare 9-digit runs are too often ids).
_SSN = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")
# 13-19 digits, optionally grouped by spaces or dashes, checked with Luhn below.
_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
# A phone number: an optional + and country code, then at least three digit groups split by
# spaces, dots, dashes or a parenthesised area code. Dates and times never have three groups of
# this shape (2026-07-01 has a two-digit tail; 23:30:00 uses colons).
_PHONE = re.compile(r"(?<![\w+.-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)\s?|\d{2,4}[\s.-])\d{3,4}[\s.-]\d{3,4}(?![\w.-])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b")


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group())
    return "[masked payment]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group()


def mask_text(text: str) -> tuple[str, int]:
    """(text with personal data replaced, number of replacements)."""
    count = 0

    def sub(pattern: re.Pattern, repl, value: str) -> str:
        nonlocal count
        def one(m: re.Match) -> str:
            nonlocal count
            out = repl(m) if callable(repl) else repl
            count += out != m.group()
            return out
        return pattern.sub(one, value)

    text = sub(_EMAIL, "[masked email]", text)
    text = sub(_IBAN, "[masked payment]", text)
    text = sub(_CARD, _card, text)
    text = sub(_SSN, "[masked government_id]", text)
    text = sub(_PHONE, "[masked phone]", text)
    return text, count


# Fields that are identifiers of the result itself, never data.
_SKIP_KEYS = frozenset({"schema_fingerprint", "database"})


def mask_result(data: Any) -> tuple[Any, int]:
    """Mask every string in a tool result. Returns the masked copy and the number of values masked."""
    total = 0

    def walk(value: Any, key: str | None = None) -> Any:
        nonlocal total
        if isinstance(value, str) and key not in _SKIP_KEYS:
            out, n = mask_text(value)
            total += n
            return out
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        return value

    return walk(data), total
