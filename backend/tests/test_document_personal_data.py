"""Personal data in documents is masked once, at extraction, before passages are stored or
embedded: whole columns of tables whose header (or whose table, for bare names and locations)
says they hold personal data, and emails, phone numbers and the like in any text. Agents, the
embedding provider, transcripts and traces only see the masked copy; the file downloads as
uploaded."""

import io
import sys
from pathlib import Path

import pytest

from app.adapters.embeddings import FakeEmbedder
from app.adapters.extraction import extract
from app.adapters.llm import ScriptedChatModel
from app.adapters.personal_data import column_kind, personal_data_kind
from app.adapters.vision import FakeImageDescriber
from tests.test_images import png

MCP_DIR = Path(__file__).resolve().parents[2] / "mcp"
sys.path.insert(0, str(MCP_DIR))
from mysql_source import masking as server_masking  # noqa: E402

CSV = "text/csv"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

CASES = [
    ("customers", "email"), ("customers", "email_hash"), ("customers", "phone_number"), ("customers", "first_name"),
    ("customers", "name"), ("customers", "city"), ("orders", "shipping_address"), ("orders", "billing_zip"),
    ("employees", "ssn"), ("employees", "date_of_birth"), ("payments", "card_number"), ("sessions", "ip_address"),
    ("users", "password_hash"), ("stores", "name"), ("stores", "city"), ("products", "product_name"),
    ("stores", "store_name"), ("customers", "customer_number"), ("customers", "region"), ("guests", "nickname"),
    ("vendor_contacts", "zip"), ("members", "gender"), ("orders", "contact_name"), ("staff", "address_2"),
]


@pytest.mark.parametrize(("table", "column"), CASES)
def test_the_backend_and_the_server_use_the_same_column_rules(table, column):
    """The MCP server runs as its own process, so the rules are copied; they must not drift."""
    assert personal_data_kind(table, column) == server_masking.personal_data_kind(table, column)
    for comment in ("Customer email address", "internal code", None):
        assert personal_data_kind(table, column, comment) == server_masking.personal_data_kind(table, column, comment)


def test_document_headers_are_read_as_column_names():
    assert column_kind("Customers", "Name") == "person_name"
    assert column_kind("Customer list (Q3)", "City") == "address"
    assert column_kind("Stores", "City") is None  # a store's city is business data
    assert column_kind("report", "Customer E-mail") == "email"
    assert column_kind("report", "Phone Number") == "phone"
    assert column_kind("report", "Date of Birth") == "birth_date"
    assert column_kind("report", "Net Sales") is None


def _texts(result):
    return "\n".join(p.text for p in result.passages)


def test_csv_columns_are_masked_by_header_and_file_name():
    data = b"Customer Name,Email,City,Order Total\nAna Ruiz,ana@example.com,Lyon,120.50\nBo Chen,bo@example.org,Oslo,80\n"
    result = extract(data, CSV, "customers.csv")
    text = _texts(result)
    for value in ("Ana Ruiz", "ana@example.com", "Lyon", "Bo Chen", "Oslo"):
        assert value not in text
    assert "Customer Name=[masked person_name]" in text and "Email=[masked email]" in text
    assert "City=[masked address]" in text and "Order Total=120.50" in text  # numbers stay
    assert "Customer Name | Email | City | Order Total" in text  # the header stays
    assert result.masked == 6

    stores = extract(b"Name,City\nHarbor,Lyon\n", CSV, "stores.csv")
    assert "Name=Harbor | City=Lyon" in _texts(stores) and stores.masked == 0


def test_spreadsheet_columns_are_masked_per_region_and_sheet():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Customers"
    ws.append(["Loyalty extract"])  # a title row; the header is the next row
    ws.append(["Name", "Phone", "Tier", "Spend"])
    ws.append(["Ana Ruiz", "+33 6 12 34 56 78", "Gold", 1200])
    ws.append(["Bo Chen", "555-201-3344", "Silver", 300])
    stores = wb.create_sheet("Stores")
    stores.append(["Name", "City"])
    stores.append(["Harbor", "Lyon"])
    buf = io.BytesIO()
    wb.save(buf)

    result = extract(buf.getvalue(), XLSX)
    text = _texts(result)
    for value in ("Ana Ruiz", "+33 6 12 34 56 78", "Bo Chen", "555-201-3344"):
        assert value not in text
    assert "A3=[masked person_name]" in text and "B3=[masked phone]" in text
    assert "C3=Gold" in text and "D3=1200" in text
    assert "A2=Name" in text and "A1=Loyalty extract" in text  # header and title stay
    assert "A2=Harbor | B2=Lyon" in text  # another sheet, business data
    assert result.masked == 4


def test_word_table_columns_are_masked():
    import docx

    document = docx.Document()
    document.add_paragraph("Escalation contacts for the returns process.")
    table = document.add_table(rows=3, cols=3)
    for row, values in zip(table.rows, [["Contact Name", "Phone", "Role"], ["Ana Ruiz", "555-201-3344", "Returns lead"],
                                        ["Bo Chen", "555-201-9988", "Finance"]]):
        for cell, value in zip(row.cells, values):
            cell.text = value
    buf = io.BytesIO()
    document.save(buf)

    result = extract(buf.getvalue(), DOCX, "contacts.docx")
    text = _texts(result)
    assert "Ana Ruiz" not in text and "555-201-9988" not in text
    assert "[masked person_name] | [masked phone] | Returns lead" in text
    assert "Contact Name | Phone | Role" in text and result.masked == 4


# --- Through upload: stored passages, embeddings, the version, the original file -------------------------


@pytest.fixture
def proj(as_user, client):
    alice = as_user("auth0|alice")
    tenant = alice.post("/tenants", json={"name": "Northstar"}).json()["id"]
    project = alice.post(f"/tenants/{tenant}/projects", json={"name": "Discovery"}).json()["id"]

    class P:
        pass

    p = P()
    p.alice, p.client = alice, client
    p.base = f"/tenants/{tenant}/projects/{project}"

    def upload(name, data):
        r = alice.post(f"{p.base}/documents", files={"file": (name, data, "application/octet-stream")})
        assert r.status_code == 201, r.text
        return r.json()["latest"]

    p.upload = upload
    return p


class RecordingEmbedder(FakeEmbedder):
    def __init__(self):
        super().__init__(1536)
        self.seen: list[str] = []

    def embed(self, texts):
        self.seen.extend(texts)
        return super().embed(texts)


MEMO = (b"Returns policy memo.\nQuestions go to ana.ruiz@example.com or +1 (415) 555-0134.\n\n"
        b"Refund card 4111 1111 1111 1111 was used in the pilot.\nNet sales exclude freight.")


def test_uploaded_text_is_masked_before_it_is_stored_or_embedded(proj):
    embedder = RecordingEmbedder()
    proj.client.app.state.embedder = embedder
    v = proj.upload("returns-memo.txt", MEMO)
    assert v["status"] == "ready" and v["personal_data_masked"] == 3
    assert any("3 personal data value(s) masked" in n for n in v["notes"])

    passages = proj.alice.get(f"{proj.base}/document-versions/{v['id']}/passages").json()
    stored = "\n".join(p["text"] for p in passages)
    sent = "\n".join(embedder.seen)
    for secret in ("ana.ruiz@example.com", "555-0134", "4111 1111 1111 1111"):
        assert secret not in stored and secret not in sent
    assert "[masked email]" in stored and "[masked phone]" in stored and "[masked payment]" in stored
    assert "Net sales exclude freight." in stored  # the business content stays

    # The person can still download what they uploaded.
    assert proj.alice.get(f"{proj.base}/document-versions/{v['id']}/content").content == MEMO


def test_a_document_without_personal_data_reports_none(proj):
    v = proj.upload("glossary.txt", b"Active customer means purchased in the last 12 months.")
    assert v["personal_data_masked"] == 0 and not any("masked" in n for n in v["notes"])


def test_an_image_transcription_is_masked_and_the_image_isnt_resent(proj, single_agent_conversation):
    proj.client.app.state.image_describer = FakeImageDescriber(
        "## Overview\nSupport dashboard. Escalate to ana.ruiz@example.com.\n- KPI Open tickets 42")
    image = proj.upload("support.png", png())
    assert image["status"] == "ready" and image["personal_data_masked"] == 1
    passages = proj.alice.get(f"{proj.base}/document-versions/{image['id']}/passages").json()
    assert "ana.ruiz@example.com" not in passages[0]["text"] and "[masked email]" in passages[0]["text"]

    proj.client.app.state.image_describer = FakeImageDescriber("## Overview\nA chart of weekly sales.")
    clean = proj.upload("chart.png", png(color=(10, 10, 10)))
    conv = single_agent_conversation(proj.base, "auth0|alice")
    model = ScriptedChatModel([
        {"tool_calls": [{"name": "view_image", "input": {"version_id": image["id"]}}]},
        {"tool_calls": [{"name": "view_image", "input": {"version_id": clean["id"]}}]},
        {"text": "Done."},
    ])
    proj.client.app.state.chat_model_factory = lambda *_: model
    assert proj.alice.post(f"{proj.base}/conversations/{conv['id']}/messages", json={"text": "Look"}).status_code == 200
    tool_turns = [t for t in model.calls[2]["transcript"] if t["role"] == "tool"]
    refused, viewed = tool_turns[0]["results"][0], tool_turns[1]["results"][0]
    assert refused["is_error"] and "isn't sent to the model again" in refused["content"] and "image" not in refused
    assert not viewed["is_error"] and viewed["image"]["media_type"] == "image/png"


def test_versions_from_before_masking_are_reprocessed(proj, db_session_factory):
    import uuid

    from app.domain.documents import needs_reprocessing
    from app.persistence.documents import DocumentVersion

    v = proj.upload("returns-memo.txt", MEMO)
    embedder = proj.client.app.state.embedder
    with db_session_factory() as s:
        version = s.get(DocumentVersion, uuid.UUID(v["id"]))
        assert not needs_reprocessing(version, embedder, None)
        version.personal_data_masked = None  # as migration 0022 leaves earlier versions
        assert needs_reprocessing(version, embedder, None)
