"""Masking of personal data before it's stored as evidence or passages, or shown to an agent.

Two layers, for sources and documents alike:
- Columns (`column_kind`): a column whose name, or whose table, says it holds personal data is
  masked whole. For sources the MCP server does this (`mcp/mysql_source/masking.py`); documents
  use the same rules here on spreadsheet, CSV and DOCX table headers. The server runs as its own
  process and doesn't import the backend, so the rules are copied, and
  `tests/test_personal_data.py` fails if the two copies disagree.
- Values (`mask_text`, below): personal data a column's name doesn't announce.

Value-level masking of personal data in source tool results is the second layer after the MCP
server's column masking.

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
# A dot may end the sentence after it, but not continue a number (10.0.0.1, 12.50).
_PHONE = re.compile(r"(?<![\w+.-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)\s?|\d{2,4}[\s.-])\d{3,4}[\s.-]\d{3,4}(?![\w-]|\.\d)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b")


# --- Columns: the rules of mcp/mysql_source/masking.py, kept in step by a test ----------------

# (kind, pattern over the column name split into snake_case words). Order matters: the first match names the kind.
_NAME_RULES: list[tuple[str, re.Pattern]] = [(k, re.compile(p, re.I)) for k, p in [
    ("email", r"(^|_)e_?mail(_|$)|(^|_)email"),
    ("phone", r"(^|_)(phone|mobile|fax|telephone|tel)(_|$)|(^|_)phone"),
    ("government_id", r"(^|_)(ssn|sin|nin|social_security|national_id|tax_id|tin|passport|driver_?s?_licen[cs]e|"
                      r"licen[cs]e_number|nhs_number|ni_number)(_|$)"),
    ("payment", r"(^|_)(card_number|cc_number|credit_card|pan|iban|bic|swift|account_number|routing_number|"
                r"sort_code|cvv|cvc)(_|$)"),
    ("secret", r"(^|_)(password|passwd|pwd|secret|api_key|token|password_hash|pin)(_|$)"),
    ("birth_date", r"(^|_)(dob|birth_?date|date_of_birth|birthday|birth_year)(_|$)"),
    ("person_name", r"(^|_)(first|last|middle|given|family|full|maiden|nick)_?name(_|$)|(^|_)(surname|forename)(_|$)|"
                    r"(^|_)(customer|contact|person|employee|user|member|patient|recipient|billing|shipping|"
                    r"cardholder|account_holder)_name(_|$)|(^|_)user_?name(_|$)"),
    ("address", r"(^|_)(home|billing|shipping|delivery|mailing|residential|customer|contact|employee)_"
                r"(address|addr|street|city|town|post_?code|postal_code|zip|zip_?code|lat|latitude|lng|lon|longitude)"
                r"|(^|_)(street|address_line_?\d?|addr_?\d|street_address|house_number|apartment|apt)(_|$)"),
    ("network", r"(^|_)(ip|ip_address|ipv4|ipv6|mac_address|device_id|user_agent|cookie_id)(_|$)"),
    ("demographic", r"(^|_)(gender|sex|ethnicity|race|religion|nationality|marital_status|health|diagnosis)(_|$)"),
]]
_COMMENT = re.compile(r"\b(pii|personal(ly)? (data|information|identifiable)|gdpr|e-?mail|phone|"
                      r"home address|date of birth|social security|passport)\b", re.I)
_PEOPLE_TABLE = re.compile(r"(customer|client|person|people|employee|staff|user|member|contact|patient|"
                           r"guest|recipient|account_holder|cardholder|subscriber|student|driver|vendor_contact)",
                           re.I)
_BARE_NAME = re.compile(r"^(name|display_name|nickname)$", re.I)
_PEOPLE_LOCATION = re.compile(r"^(address|address_\d|addr|city|town|post_?code|postal_code|zip|zip_?code|lat|latitude|"
                              r"lng|lon|longitude|geo|geolocation|coordinates)$", re.I)


def personal_data_kind(table: str, column: str, comment: str | None = None) -> str | None:
    """The kind of personal data a column holds ('email', 'person_name', ...), or None. Same
    rules as the MCP server's."""
    for kind, pattern in _NAME_RULES:
        if pattern.search(column):
            return kind
    if _PEOPLE_TABLE.search(table):
        if _BARE_NAME.match(column):
            return "person_name"
        if _PEOPLE_LOCATION.match(column):
            return "address"
    if comment and _COMMENT.search(comment):
        return "personal"
    return None


def snake(header: str) -> str:
    """A document header as a column name: "Customer E-mail" -> "customer_e_mail"."""
    return re.sub(r"[^0-9a-z]+", "_", header.strip().lower()).strip("_")


def column_kind(table: str, header: str) -> str | None:
    """The personal-data kind of a document table column, from its header and the table's name
    (a sheet name, or the file's name for CSV and Word tables)."""
    column = snake(header)
    return personal_data_kind(snake(table), column) if column else None


def marker(kind: str) -> str:
    return f"[masked {kind}]"


# --- Values --------------------------------------------------------------------------------


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
