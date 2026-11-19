"""Personal data in source results: the MCP server masks personal-data columns, and the backend
adapter masks personal data in any other text before evidence is stored or an agent sees it."""

import sys
from typing import Any
from pathlib import Path

import pytest

from app.adapters.personal_data import mask_result, mask_text
from app.adapters.sources import ConnectionParams, InProcessConnector
from tests.test_sources import NORTHSTAR_PASSWORD, _in_process_connector, _northstar_params, needs_mysql

MCP_DIR = Path(__file__).resolve().parents[2] / "mcp"
sys.path.insert(0, str(MCP_DIR))
from mysql_source.masking import mask, personal_data_kind  # noqa: E402


@pytest.mark.parametrize(("table", "column", "comment", "kind"), [
    ("customers", "email", None, "email"),
    ("customers", "email_hash", None, "email"),  # a hash still links one person across systems
    ("customers", "phone_number", None, "phone"),
    ("customers", "first_name", None, "person_name"),
    ("customers", "name", None, "person_name"),
    ("customers", "city", None, "address"),
    ("orders", "shipping_address", None, "address"),
    ("orders", "billing_zip", None, "address"),
    ("employees", "ssn", None, "government_id"),
    ("employees", "date_of_birth", None, "birth_date"),
    ("payments", "card_number", None, "payment"),
    ("sessions", "ip_address", None, "network"),
    ("users", "password_hash", None, "secret"),
    ("orders", "contact", "Customer email address", "personal"),
    # Business data stays visible.
    ("stores", "name", None, None),
    ("stores", "city", None, None),
    ("products", "product_name", None, None),
    ("stores", "store_name", None, None),
    ("customers", "customer_number", None, None),
    ("customers", "region", None, None),
    ("customers", "home_store_id", None, None),
    ("customers", "signup_date", None, None),
    ("orders", "freight_charged", None, None),
    ("order_lines", "discount_per_unit", "Discount per unit", None),
    ("products", "headphone_type", None, None),
])
def test_personal_data_columns_are_detected(table, column, comment, kind):
    assert personal_data_kind(table, column, comment) == kind


def test_masking_keeps_null_and_leaves_other_columns_alone():
    assert mask("ana@example.com", "email") == "[masked email]"
    assert mask(None, "email") is None
    assert mask("Harbor", None) == "Harbor"


@pytest.mark.parametrize(("text", "expected"), [
    ("Call Ana at +1 (415) 555-0134 or ana.ruiz+orders@example.co.uk",
     "Call Ana at [masked phone] or [masked email]"),
    ("refund to card 4111 1111 1111 1111 ok", "refund to card [masked payment] ok"),
    ("SSN 078-05-1120 on file", "SSN [masked government_id] on file"),
    ("IBAN GB82 WEST 1234 5698 7654 32", "IBAN [masked payment]"),
    ("tel 415.555.0134", "tel [masked phone]"),
    ("Questions go to +1 (415) 555-0134.", "Questions go to [masked phone]."),  # ends a sentence
])
def test_personal_data_in_text_is_masked(text, expected):
    assert mask_text(text)[0] == expected


@pytest.mark.parametrize("text", [
    "2026-07-01", "2026-07-01T23:30:00", "2026-07-01 23:30:00", "514748.44", "MS-500001", "C000123",
    "3671d132-c57b-4bce-8019-8d0d51f34fe2", "1234567890123456",  # 16 digits failing Luhn: an id, not a card
    "recount after cycle-count correction", "quantity × (unit_price − discount_per_unit)", "v1 lines 8-12",
    "host 10.20.300.4000.5", "1200 3400 5600.75",  # dots that continue a number aren't a sentence end
])
def test_ordinary_values_are_not_masked(text):
    assert mask_text(text) == (text, 0)


def test_results_are_masked_everywhere_but_their_own_identifiers():
    data = {"schema_fingerprint": "a" * 64, "database": "ops", "rows": [[1, "ana@example.com", None, 9.5]],
            "comment": "owner: ops-team@example.com"}
    masked, n = mask_result(data)
    assert n == 2
    assert masked["rows"] == [[1, "[masked email]", None, 9.5]]
    assert masked["comment"] == "owner: [masked email]" and masked["schema_fingerprint"] == "a" * 64


def test_adapter_masks_tool_results_before_they_are_returned():
    """The MCP client path (`_McpSession`) masks what the server returned, so nothing unmasked is
    stored as evidence or reaches an agent, even from a server that doesn't mask."""
    from mcp.server.mcpserver import MCPServer

    def server(_params):
        s = MCPServer(name="leaky")

        @s.tool()
        def sample_rows(table: str, limit: int = 10, columns: list[str] | None = None) -> dict[str, Any]:
            return {"schema_fingerprint": "f" * 64, "table": table, "columns": ["id", "note"],
                    "rows": [[1, "Customer asked to be called on 415-555-0134"], [2, "ok"]]}

        return s

    with InProcessConnector(server).open(ConnectionParams("h", 1, "d", "u", "p")) as session:
        result = session.call("sample_rows", {"table": "orders"})
    assert not result.is_error
    assert result.data["rows"] == [[1, "Customer asked to be called on [masked phone]"], [2, "ok"]]
    assert result.data["personal_data_masked"] == 1


@needs_mysql
def test_northstar_customer_emails_never_leave_the_server():
    assert NORTHSTAR_PASSWORD
    with _in_process_connector().open(_northstar_params()) as session:
        described = session.call("describe_table", {"table": "customers"}).data
        flags = {c["name"]: c["personal_data"] for c in described["columns"]}
        assert flags["email_hash"] == "email" and flags["customer_number"] is None

        sample = session.call("sample_rows", {"table": "customers", "limit": 5}).data
        i = sample["columns"].index("email_hash")
        assert {row[i] for row in sample["rows"]} == {"[masked email]"}
        assert {"column": "email_hash", "kind": "email"} in sample["masked_columns"]
        assert all(row[sample["columns"].index("customer_number")].startswith("C") for row in sample["rows"])

        profile = session.call("column_profile", {"table": "customers", "columns": ["email_hash", "signup_date"]}).data
        email, signup = profile["columns"]
        assert email["personal_data"] == "email" and email["distinct"] > 100  # shape is kept
        assert not {"top_values", "min", "max"} & email.keys()
        assert "min" in signup  # other columns are profiled as before

        key = session.call("candidate_key_check", {"table": "customers", "columns": ["email_hash"]}).data
        assert all(k == ["[masked email]"] for d in key["example_duplicates"] for k in [d["key"]])

        cov = session.call("coverage_check", {"table": "customers", "columns": ["signup_date"],
                                              "group_by": ["email_hash"]}).data
        assert cov["example_incomplete_groups"] and {"column": "email_hash", "kind": "email"} in cov["masked_columns"]
        assert all(g["group"] == ["[masked email]"] for g in cov["example_incomplete_groups"])
