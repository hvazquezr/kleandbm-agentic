"""Which columns hold personal data, and how their values are masked before they leave the server.

Modeling needs a column's shape (type, nulls, distinct counts, whether it is unique), not the
people in it. So a personal-data column keeps its counts and rates, while its values (samples,
top values, min and max, duplicate-key examples) are replaced by a marker naming the kind of data.
NULL stays NULL, because whether a value is present is shape, not content.

Detection is by column name, by column comment, and by bare `name`-style columns in tables that
describe people. It is deliberately broad: masking a column that isn't personal costs an agent a
sample, while missing one sends personal data to a model provider. The backend adds a second,
value-level pass (`app/adapters/personal_data.py`) for personal data in columns that aren't named
for it, such as an email inside a free-text note.
"""

import re

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
    # A person's own address or position; street-level parts are always personal.
    ("address", r"(^|_)(home|billing|shipping|delivery|mailing|residential|customer|contact|employee)_"
                r"(address|addr|street|city|town|post_?code|postal_code|zip|zip_?code|lat|latitude|lng|lon|longitude)"
                r"|(^|_)(street|address_line_?\d?|addr_?\d|street_address|house_number|apartment|apt)(_|$)"),
    ("network", r"(^|_)(ip|ip_address|ipv4|ipv6|mac_address|device_id|user_agent|cookie_id)(_|$)"),
    ("demographic", r"(^|_)(gender|sex|ethnicity|race|religion|nationality|marital_status|health|diagnosis)(_|$)"),
]]

# Comments that say a column is personal, e.g. "Customer email" or "PII".
_COMMENT = re.compile(r"\b(pii|personal(ly)? (data|information|identifiable)|gdpr|e-?mail|phone|"
                      r"home address|date of birth|social security|passport)\b", re.I)

# Tables that describe people, where a bare `name` column is a person's name.
_PEOPLE_TABLE = re.compile(r"(customer|client|person|people|employee|staff|user|member|contact|patient|"
                           r"guest|recipient|account_holder|cardholder|subscriber|student|driver|vendor_contact)",
                           re.I)
_BARE_NAME = re.compile(r"^(name|display_name|nickname)$", re.I)
# In those tables, location columns place a person: a store's city is business data, a customer's isn't.
_PEOPLE_LOCATION = re.compile(r"^(address|address_\d|addr|city|town|post_?code|postal_code|zip|zip_?code|lat|latitude|"
                              r"lng|lon|longitude|geo|geolocation|coordinates)$", re.I)


def personal_data_kind(table: str, column: str, comment: str | None = None) -> str | None:
    """The kind of personal data a column holds ('email', 'person_name', ...), or None."""
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


def marker(kind: str) -> str:
    return f"[masked {kind}]"


def mask(value, kind: str | None):
    """A value as it may leave the server: unchanged if the column isn't personal, NULL kept."""
    if kind is None or value is None:
        return value
    return marker(kind)
