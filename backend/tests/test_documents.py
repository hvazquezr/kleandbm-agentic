"""Document storage, extraction with locations, versioning, search, and isolation."""

import io
from pathlib import Path

import pytest

from app.adapters.embeddings import FakeEmbedder
from app.adapters.extraction import content_type_for, extract
from app.config import get_settings

NORTHSTAR = Path(__file__).resolve().parents[2] / "retail-discovery" / "northstar-current-state-report.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --- Fixture builders -----------------------------------------------------------------------


def make_pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF with one line of Helvetica text per page."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font_id = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


def make_docx() -> bytes:
    import docx

    d = docx.Document()
    d.add_heading("Glossary", level=1)
    d.add_paragraph("Active customer: a customer with at least one completed order in the last 90 days.")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Term", "Meaning"
    table.cell(1, 0).text, table.cell(1, 1).text = "Net revenue", "Gross sales less discounts"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_xlsx_without_cached_values() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Calc"
    ws["A1"], ws["B1"], ws["C1"] = "Gross", "Discounts", "Net"
    ws["A2"], ws["B2"], ws["C2"] = 100, 10, "=A2-B2"  # openpyxl writes no cached result
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- Extraction (no database) ---------------------------------------------------------------


def test_content_type_prefers_extension_over_browser_type():
    assert content_type_for("report.xlsx", "application/octet-stream") == XLSX
    assert content_type_for("notes.md", None) == "text/markdown"
    assert content_type_for("blob", "text/plain; charset=utf-8") == "text/plain"


def test_pdf_passages_carry_page_numbers():
    result = extract(make_pdf(["Net revenue excludes tax", "Returns are net of discounts"]), "application/pdf")
    assert [p.location["page"] for p in result.passages] == [1, 2]
    assert "Returns are net of discounts" in result.passages[1].text
    loc = result.passages[1].location
    assert loc["char_end"] - loc["char_start"] == len(result.passages[1].text)


def test_docx_paragraphs_and_tables_are_located():
    result = extract(make_docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    paragraph = next(p for p in result.passages if "paragraphs" in p.location)
    assert paragraph.text.startswith("# Glossary") and "Active customer" in paragraph.text
    table = next(p for p in result.passages if "table" in p.location)
    assert table.location == {"table": 1, "rows": [1, 2]}
    assert "Net revenue | Gross sales less discounts" in table.text


def test_xlsx_formula_without_cached_value_is_flagged():
    result = extract(make_xlsx_without_cached_values(), XLSX)
    assert "C2==A2-B2 (no cached value)" in result.passages[0].text
    assert any("no cached value" in n for n in result.notes)


@pytest.mark.skipif(not NORTHSTAR.exists(), reason="retail-discovery workbook not present")
def test_northstar_workbook_keeps_definitions_headers_and_exact_ranges():
    result = extract(NORTHSTAR.read_bytes(), XLSX)
    by_range = {(p.location["sheet"], p.location["range"]): p for p in result.passages}
    sales = by_range[("Sales Report", "B7:H12")]
    # Short formulas are business definitions and stay verbatim; long lookups are summarized.
    assert "F8=123,690 [=D8-E8]" in sales.text
    assert "[=SUMIFS over 'Monthly Extract']" in sales.text
    # A continuation piece names exactly its own rows and repeats the header for context.
    tail = by_range[("Sales Report", "B13:H14")]
    assert tail.location["header_rows"] == [7]
    assert "Gross sales" in tail.text and "937,602" in tail.text
    # A section title above a split table is repeated together with the column header.
    finance = next(p for p in result.passages if p.location.get("header_rows") == [16, 17])
    assert "Finance net revenue by product category" in finance.text and "Category" in finance.text
    # Notes that define the competing meanings of net revenue are extracted.
    texts = "\n".join(p.text for p in result.passages)
    assert "Sales net revenue = gross sales less discounts" in texts
    assert "Finance net revenue deducts both discounts and returns" in texts


# --- API ------------------------------------------------------------------------------------


@pytest.fixture
def proj(as_user):
    alice = as_user("auth0|alice")
    tenant = alice.post("/tenants", json={"name": "Northstar"}).json()["id"]
    project = alice.post(f"/tenants/{tenant}/projects", json={"name": "Discovery"}).json()["id"]

    class P:
        pass

    p = P()
    p.alice, p.tenant, p.project = alice, tenant, project
    p.base = f"/tenants/{tenant}/projects/{project}"

    def upload(user, name, data, url=None, **form):
        return user.post(url or f"{p.base}/documents", files={"file": (name, data, "application/octet-stream")}, data=form)

    p.upload = upload
    return p


def test_upload_extracts_embeds_and_searches_with_locations(proj, object_store):
    pdf = make_pdf(["Profit by order requires order level cost detail", "Store costs are recorded per store per day"])
    resp = proj.upload(proj.alice, "costs.pdf", pdf, title="Cost notes")
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    v = doc["latest"]
    assert doc["title"] == "Cost notes"
    assert v["status"] == "ready" and v["version_no"] == 1 and v["embedding_model"] == "fake-hash-v1"
    assert "storage_key" not in v

    passages = proj.alice.get(f"{proj.base}/document-versions/{v['id']}/passages").json()
    assert [p["location"]["page"] for p in passages] == [1, 2]

    hits = proj.alice.post(f"{proj.base}/search", json={"query": "costs per store per day", "limit": 1}).json()
    assert hits[0]["location"]["page"] == 2 and hits[0]["document_title"] == "Cost notes"

    content = proj.alice.get(f"{proj.base}/document-versions/{v['id']}/content")
    assert content.content == pdf
    assert content.headers["content-disposition"].startswith("attachment;")
    assert content.headers["x-content-type-options"] == "nosniff"


def test_versions_are_immutable_and_search_uses_latest(proj):
    first = proj.upload(proj.alice, "glossary.txt", b"Active customer means purchased in the last 12 months.").json()
    url = f"{proj.base}/documents/{first['id']}/versions"
    same = proj.upload(proj.alice, "glossary.txt", b"Active customer means purchased in the last 12 months.", url=url)
    assert same.status_code == 409  # identical content isn't a new version

    second = proj.upload(proj.alice, "glossary.txt", b"Active customer means purchased in the last 90 days.", url=url)
    assert second.status_code == 201 and second.json()["version_no"] == 2
    versions = proj.alice.get(url).json()
    assert [v["version_no"] for v in versions] == [2, 1]

    hits = proj.alice.post(f"{proj.base}/search", json={"query": "active customer months days"}).json()
    assert {h["version_no"] for h in hits} == {2}
    assert "90 days" in hits[0]["text"]
    # The superseded version stays downloadable for traceability.
    old = proj.alice.get(f"{proj.base}/document-versions/{versions[1]['id']}/content")
    assert b"12 months" in old.content


def test_storage_keys_use_internal_ids_not_client_paths(proj, object_store):
    resp = proj.upload(proj.alice, "../../../etc/passwd.txt", b"harmless text")
    assert resp.status_code == 201
    assert resp.json()["latest"]["filename"] == "passwd.txt"
    stored = [p for p in Path(object_store.root).rglob("*") if p.is_file()]
    assert len(stored) == 1
    rel = stored[0].relative_to(object_store.root).as_posix()
    assert rel.startswith(f"tenants/{proj.tenant}/projects/{proj.project}/documents/")
    assert rel.endswith("/v1/original.txt") and "passwd" not in rel


def test_unsupported_empty_oversized_and_image_uploads(proj, monkeypatch):
    assert proj.upload(proj.alice, "tool.exe", b"MZ...").status_code == 409
    assert proj.upload(proj.alice, "empty.txt", b"").status_code == 409
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 10)
    assert proj.upload(proj.alice, "big.txt", b"x" * 11).status_code == 409
    monkeypatch.undo()

    png = proj.upload(proj.alice, "Sales-Report.png", b"\x89PNG\r\n\x1a\nfake").json()["latest"]
    assert png["status"] == "stored_only" and png["notes"]


def test_extraction_failure_keeps_the_file(proj):
    v = proj.upload(proj.alice, "broken.pdf", b"%PDF-1.4 this is not really a pdf").json()["latest"]
    assert v["status"] == "extraction_failed" and v["status_detail"]
    assert proj.alice.get(f"{proj.base}/document-versions/{v['id']}/content").status_code == 200


def test_embedding_failure_can_be_retried(proj, client):
    class Down(FakeEmbedder):
        def embed(self, texts):
            raise RuntimeError("provider unavailable")

    client.app.state.embedder = Down(1536)
    v = proj.upload(proj.alice, "notes.txt", b"Returns are net of original discounts.").json()["latest"]
    assert v["status"] == "embedding_failed" and "provider unavailable" in v["status_detail"]

    client.app.state.embedder = FakeEmbedder(1536)
    assert proj.alice.post(f"{proj.base}/search", json={"query": "returns discounts"}).json() == []
    retried = proj.alice.post(f"{proj.base}/document-versions/{v['id']}/reembed").json()
    assert retried["status"] == "ready"
    assert proj.alice.post(f"{proj.base}/search", json={"query": "returns discounts"}).json()


def test_documents_are_tenant_and_role_scoped(proj, as_user):
    v = proj.upload(proj.alice, "secret.txt", b"Northstar margin targets are confidential.").json()["latest"]

    bob = as_user("auth0|bob")
    assert bob.get(f"{proj.base}/documents").status_code == 404
    assert bob.get(f"{proj.base}/document-versions/{v['id']}/content").status_code == 404
    assert bob.post(f"{proj.base}/search", json={"query": "margin"}).status_code == 404

    # Bob's own project can't reach Alice's version id, and its search never returns her passages.
    tb = bob.post("/tenants", json={"name": "Globex"}).json()["id"]
    pb = bob.post(f"/tenants/{tb}/projects", json={"name": "Ops"}).json()["id"]
    bob_base = f"/tenants/{tb}/projects/{pb}"
    assert bob.get(f"{bob_base}/document-versions/{v['id']}/content").status_code == 404
    assert bob.get(f"{bob_base}/document-versions/{v['id']}/passages").status_code == 404
    assert bob.post(f"{bob_base}/search", json={"query": "Northstar margin targets"}).json() == []

    carol = as_user("auth0|carol")
    proj.alice.put(f"/tenants/{proj.tenant}/members/{carol.id}", json={"role": "member"})
    proj.alice.put(f"{proj.base}/members/{carol.id}", json={"role": "viewer"})
    assert carol.post(f"{proj.base}/search", json={"query": "margin"}).json()
    assert carol.get(f"{proj.base}/document-versions/{v['id']}/content").status_code == 200
    assert proj.upload(carol, "mine.txt", b"viewer upload").status_code == 403
