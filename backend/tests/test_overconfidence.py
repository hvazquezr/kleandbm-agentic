"""Deterministic checks on confident claims: citations must name documents the run actually read,
source_refs must name tables and columns the source tools showed, fully_supported needs sourced
mappings, and a design run with a linked source must have looked at it. No model judges these."""

from types import SimpleNamespace

import pytest

from app.agents.requirements import tools as rt
from app.agents.runtime import ToolInputError
from app.domain import design
from app.domain.design import SourceSchema
from tests.test_architect import DIM_STORE, FACT_SALES, REL, _tool_results, design_steps, proj  # noqa: F401


def _attr(name, ref="", role="descriptive"):
    return {"name": name, "logical_type": "text", "role": role, "is_nullable": True, "additivity": "none",
            "description": "", "source_ref": ref}


def _content(*tables):
    return {"tables": list(tables), "relationships": [], "mappings": []}


def _table(name, ref, *attrs, kind="dimension"):
    return {"name": name, "kind": kind, "grain": "one row per thing", "description": "", "source_ref": ref,
            "attributes": [_attr(f"{name}_key", role="surrogate_key"), *attrs]}


SCHEMA = SourceSchema(tables={"shipments": {"shipment_id", "carrier", "order_id"}, "orders": None},
                      tables_complete=True)


def _source_checks(content, schema):
    return {c["name"]: c for c in design.internal_checks(content, [], schema) if c["check_type"] == "assumptions"}


def test_a_source_ref_to_a_table_the_source_doesnt_have_is_a_hard_failure():
    checks = _source_checks(_content(_table("dim_carrier", "carriers", _attr("carrier_code", "carriers.code"))), SCHEMA)
    failed = checks["source_refs_exist"]
    assert failed["status"] == "failed" and failed["hard"] is True
    assert "no table carriers" in failed["summary"] and "names no table in the source" in failed["summary"]
    assert failed["elements"] == ["dim_carrier", "dim_carrier.carrier_code"]
    assert design.hard_failures(list(checks.values()))


def test_a_column_the_described_table_doesnt_have_fails_and_real_ones_pass():
    bad = _source_checks(_content(_table("dim_shipment", "shipments", _attr("carrier", "shipments.carrier_name"))), SCHEMA)
    assert "shipments doesn't have" in bad["source_refs_exist"]["summary"]
    good = _source_checks(_content(_table("dim_shipment", "shipments (evidence 1234)",
                                          _attr("carrier", "shipments.carrier"),
                                          _attr("order_ref", "shipments.order_id; see meridian-ops-notes.md"))), SCHEMA)
    assert "source_refs_exist" not in good and good["source_evidence"]["status"] == "passed"


def test_what_nobody_looked_at_is_unverified_not_failed():
    # orders was listed but never described; without list_tables, unknown tables can't be judged.
    partial = SourceSchema(tables={"shipments": {"carrier"}, "orders": None}, tables_complete=False)
    checks = _source_checks(_content(_table("dim_order", "orders", _attr("status", "orders.status")),
                                     _table("dim_store", "stores", _attr("name", "stores.name"))), partial)
    assert "source_refs_exist" not in checks
    assert checks["source_evidence"]["status"] == "unverified"
    assert set(checks["source_evidence"]["elements"]) >= {"dim_order.status", "dim_store", "dim_store.name"}
    # No schema evidence at all: unverified, never a pass, even when every table cites something.
    assert _source_checks(_content(_table("dim_store", "stores")), None)["source_evidence"]["status"] == "unverified"


def test_derived_tables_and_references_to_the_model_are_not_source_claims():
    checks = _source_checks(_content(
        _table("dim_date", "Generated calendar", _attr("month", "derived from the date")),
        _table("dim_shipment", "shipments", _attr("date_key", "dim_date.dim_date_key"), _attr("carrier", "shipments.carrier"))),
        SCHEMA)
    assert "source_refs_exist" not in checks and checks["source_evidence"]["status"] == "passed"


def test_citations_must_name_a_document_the_run_read():
    ctx = SimpleNamespace(seen_documents={"meridian-ops-notes.md"})
    rt.check_citations(ctx, ["meridian-ops-notes.md v1, lines 8-12", "Meridian ops notes, passage 3"])
    with pytest.raises(ToolInputError, match="no document you read"):
        rt.check_citations(ctx, ["meridian-ops-notes.md v1", "Finance glossary v2 · page 4"])
    with pytest.raises(ToolInputError, match="none yet"):
        rt.check_citations(SimpleNamespace(seen_documents=set()), ["meridian-ops-notes.md v1"])


def test_documented_definitions_must_name_a_project_document(monkeypatch):
    docs = [(SimpleNamespace(title="finance-glossary.md"), None)]
    monkeypatch.setattr(rt.documents, "list_documents", lambda *_: docs)
    ctx = SimpleNamespace(session=None, scope=None)
    rt.check_documented_origin(ctx, "Finance glossary v1 · page 2")
    with pytest.raises(ToolInputError, match="names no document in this project"):
        rt.check_documented_origin(ctx, "Board deck, slide 4")


def test_documents_alone_cant_make_a_requirement_fully_supported():
    with pytest.raises(ToolInputError, match="only the Architect"):
        rt.record_requirement(SimpleNamespace(), {"statement": "x", "origin_kind": "document", "origin_ref": "",
                                                  "feasibility_status": "fully_supported"})


def _feasibility(p, status="fully_supported"):
    from tests.test_architect import _seq_of

    return {"name": "update_feasibility", "input": {"requirement_id": p.revenue_req, "expected_seq": _seq_of(p, p.revenue_req),
                                                    "feasibility_status": status, "feasibility_notes": ""}}


def test_fully_supported_needs_mapped_elements_verified_against_the_source(proj):
    seen = {}

    def finish(t):
        seen["results"] = _tool_results(t)
        return {"text": "Done."}

    steps = design_steps(proj)
    proj.script(
        {"tool_calls": [_feasibility(proj)]},  # nothing mapped yet
        *steps[:3],  # look at order_lines, design dim_store and fact_sales
        {"tool_calls": [{"name": "map_requirement", "input": {"requirement_id": proj.revenue_req,
                                                              "elements": ["fact_sales.net_amount", "dim_store"],
                                                              "note": ""}}]},
        {"tool_calls": [_feasibility(proj)]},  # stores was never looked at; order_lines was
        finish,
    )
    proj.run()
    unmapped, mapped = [r for r in seen["results"] if r["is_error"]]
    assert "map the requirement" in unmapped["content"]
    assert "aren't: dim_store." in mapped["content"]  # fact_sales cites order_lines columns that exist


def test_a_design_run_that_never_looked_at_its_source_is_blocked(proj):
    proj.script(
        {"tool_calls": [{"name": "put_table", "input": DIM_STORE}, {"name": "put_table", "input": FACT_SALES}]},
        {"tool_calls": [{"name": "put_relationships", "input": {"relationships": [REL]}}]},
        {"text": "I checked the sources and the design is verified."},
    )
    done = proj.run()[-1]
    cand = proj.alice.get(f"{proj.base}/candidates/{done['candidate']['id']}").json()
    check = next(c for c in cand["internal_checks"] if c["name"] == "sources_investigated")
    assert check["status"] == "failed" and check["hard"] is True
    assert done["candidate"]["ready_for_review"] is False

    proj.script(*design_steps(proj))  # this one describes order_lines first
    done = proj.run("Again")[-1]
    cand = proj.alice.get(f"{proj.base}/candidates/{done['candidate']['id']}").json()
    assert next(c for c in cand["internal_checks"] if c["name"] == "sources_investigated")["status"] == "passed"


def _registers(p, *ops):
    r = p.alice.post(f"{p.base}/changesets", json={"operations": list(ops)})
    assert r.status_code in (200, 201), r.text
    return r.json()["created"]


def _gate_run(proj):
    """Design with dim_store described as well, map revenue, then try fully_supported."""
    seen = {}

    def finish(t):
        seen["results"] = _tool_results(t)
        return {"text": "Done."}

    steps = design_steps(proj)
    describe_stores = lambda t: {"tool_calls": [{"name": "source_describe_table", "input": {  # noqa: E731
        "source_id": next(r["content"]["sources"][0]["source_id"] for r in _tool_results(t)
                          if not r["is_error"] and "sources" in r["content"]),
        "table": "stores", "purpose": "store keys"}}]}
    proj.script(*steps[:2], describe_stores, steps[2],
                {"tool_calls": [{"name": "map_requirement", "input": {"requirement_id": proj.revenue_req,
                                                                      "elements": ["dim_store"], "note": ""}}]},
                {"tool_calls": [_feasibility(proj)]}, finish)
    proj.run()
    return [r for r in seen["results"] if r["is_error"]]


def test_an_open_blocking_question_keeps_a_requirement_from_full_support(proj):
    _registers(proj, {"op": "create", "type": "question", "ref": "q", "data": {
        "kind": "clarification", "description": "Which calendar do months follow?", "is_blocking": True}},
        {"op": "link", "from_id": "$q", "to_id": proj.revenue_req, "link_type": "concerns"})
    [refused] = _gate_run(proj)
    assert "meaning is still open" in refused["content"] and "is open and blocking" in refused["content"]


def test_a_conflicting_definition_keeps_a_requirement_from_full_support(proj):
    created = _registers(proj,
        {"op": "create", "type": "definition", "ref": "a", "data": {"term": "Net revenue", "provenance": "inferred"}},
        {"op": "create", "type": "definition", "ref": "b", "data": {"term": "Net revenue (finance)", "provenance": "inferred"}},
        {"op": "link", "from_id": "$a", "to_id": "$b", "link_type": "conflicts_with"},
        {"op": "link", "from_id": proj.revenue_req, "to_id": "$a", "link_type": "uses_definition"})
    [refused] = _gate_run(proj)
    assert "conflicts with" in refused["content"] and "Net revenue (finance)" in refused["content"]
    assert created["a"] and created["b"]


def test_without_open_meaning_and_with_verified_tables_full_support_is_allowed(proj):
    assert _gate_run(proj) == []  # dim_store's source table was described; nothing is open


def _decision(**extra):
    return {"name": "record_decision", "input": {
        "kind": "decision", "statement": "Order-line grain for sales", "rationale": "", "alternatives": [],
        "affects_ids": [], "affects_elements": ["fact_sales"], "evidence_ids": [], "citations": [], **extra}}


def test_a_decision_about_elements_needs_evidence_or_becomes_an_assumption(proj):
    from tests.test_architect import evidence_ids

    seen = {}

    def finish(t):
        seen["results"] = _tool_results(t)
        return {"text": "Done."}

    steps = design_steps(proj)
    proj.script(*steps[:3],
                {"tool_calls": [_decision()]},                                     # nothing shows it
                {"tool_calls": [_decision(evidence_ids=["not-an-id"])]},           # not an evidence id
                {"tool_calls": [_decision(citations=["Board deck v1, slide 3"])]},  # never read
                {"tool_calls": [_decision(kind="assumption", statement="Returns post in the order month")]},
                lambda t: {"tool_calls": [_decision(evidence_ids=evidence_ids(t))]},
                finish)
    proj.run()
    results = seen["results"]
    no_evidence, bad_id, unread, assumption, decision = results[-5:]
    assert no_evidence["is_error"] and 'kind "assumption"' in no_evidence["content"]
    assert bad_id["is_error"] and "isn't an evidence id" in bad_id["content"]
    assert unread["is_error"] and "no document you read" in unread["content"]
    assert not assumption["is_error"] and not decision["is_error"]

    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    by_kind = {d["data"]["kind"]: d["data"] for d in snap["decisions"]}
    assert by_kind["assumption"]["evidence"] == []
    [source] = by_kind["decision"]["evidence"]
    assert source["kind"] == "source" and source["tool"] == "describe_table"
