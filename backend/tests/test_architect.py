"""The Architect agent and design candidates: source investigation with evidence, the isolated
workspace, internal checks, applying as one changeset, proposals for non-empty models, stale
detection, authority limits, and isolation. Uses a scripted model and a fake source."""

import json
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.adapters.llm import LLMError, ScriptedChatModel
from app.config import get_settings
from app.domain import design, modeling, tenancy
from app.domain.access import Actor, ActorKind
from app.domain.modeling import CreateOp, UpdateOp
from app.domain.tenancy import InvalidOperation
from app.persistence.conversations import AgentRun
from app.persistence.design import CandidateStatus, DesignCandidate
from app.persistence.modeling import Changeset
from app.persistence.sources import SourceEvidence
from tests.test_sources import FakeConnector


def sse(resp) -> list[dict]:
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def last_result(transcript) -> dict:
    return json.loads([t for t in transcript if t["role"] == "tool"][-1]["results"][0]["content"])


@pytest.fixture
def proj(as_user, client, monkeypatch):
    monkeypatch.setattr(get_settings(), "secrets_key", Fernet.generate_key().decode())
    connector = FakeConnector()
    client.app.state.source_connector = connector
    alice = as_user("auth0|alice")
    tenant = alice.post("/tenants", json={"name": "Northstar"}).json()["id"]
    project = alice.post(f"/tenants/{tenant}/projects", json={"name": "Discovery"}).json()["id"]
    base = f"/tenants/{tenant}/projects/{project}"
    model = alice.post(f"{base}/models", json={"name": "Retail"}).json()["id"]
    source = alice.post(f"{base}/sources", json={"name": "ops", "host": "127.0.0.1", "port": 3306,
                                                 "database": "northstar_ops", "username": "ro", "password": "x"}).json()
    assert alice.put(f"{base}/models/{model}/sources/{source['id']}").status_code == 204
    req = alice.post(f"{base}/changesets", json={"operations": [
        {"op": "create", "type": "requirement", "ref": "r", "data": {
            "statement": "Monthly net revenue by store", "origin_kind": "user_statement"}},
        {"op": "create", "type": "requirement", "ref": "p", "data": {
            "statement": "Profit by order", "origin_kind": "user_statement"}},
    ]}).json()

    class P:
        pass

    p = P()
    p.alice, p.base, p.model, p.source, p.client, p.connector = alice, base, model, source["id"], client, connector
    p.revenue_req, p.profit_req = req["created"]["r"], req["created"]["p"]
    p.conv = alice.post(f"{base}/conversations", json={"model_id": model, "agent": "architect"}).json()

    def factory_for(models):
        def factory(agent):
            if agent not in models:
                raise LLMError(f"no model is configured for the {agent} agent")
            return models[agent]
        return factory

    def script(*steps):
        # The Architect and the Business Requirements subgraph share one script; no Validator.
        m = ScriptedChatModel(list(steps))
        client.app.state.chat_model_factory = factory_for({"architect": m, "business_requirements": m})
        return m

    def run(text="Design the model"):
        return sse(alice.post(f"{base}/conversations/{p.conv['id']}/messages", json={"text": text}))

    def script_agents(architect_steps, requirements_steps=(), validator_steps=None):
        a, b = ScriptedChatModel(list(architect_steps)), ScriptedChatModel(list(requirements_steps))
        models = {"architect": a, "business_requirements": b}
        if validator_steps is not None:
            models["validator"] = ScriptedChatModel(list(validator_steps))
        client.app.state.chat_model_factory = factory_for(models)
        return a, b, models.get("validator")

    p.script, p.run, p.script_agents = script, run, script_agents
    return p


DIM_STORE = {
    "name": "dim_store", "previous_name": "", "kind": "dimension", "grain": "", "description": "Stores",
    "scd_type": "type_1", "source_ref": "stores (evidence)",
    "attributes": [
        {"name": "store_key", "logical_type": "integer", "role": "surrogate_key", "is_nullable": False,
         "additivity": "none", "description": "", "source_ref": ""},
        {"name": "store_id", "logical_type": "text", "role": "natural_key", "is_nullable": False,
         "additivity": "none", "description": "", "source_ref": "stores.store_id"},
        {"name": "store_name", "logical_type": "text", "role": "descriptive", "is_nullable": True,
         "additivity": "none", "description": "", "source_ref": "stores.name"},
    ],
}
FACT_SALES = {
    "name": "fact_sales", "previous_name": "", "kind": "fact", "grain": "One row per order line",
    "description": "", "scd_type": "none", "source_ref": "order_lines",
    "attributes": [
        {"name": "store_key", "logical_type": "integer", "role": "foreign_key", "is_nullable": False,
         "additivity": "none", "description": "", "source_ref": ""},
        {"name": "net_amount", "logical_type": "decimal", "role": "measure", "is_nullable": False,
         "additivity": "additive", "description": "Net revenue", "source_ref": "order_lines.net_amount"},
    ],
}
REL = {"from_table": "fact_sales", "from_attribute": "store_key", "to_table": "dim_store",
       "to_attribute": "store_key", "cardinality": "many_to_one", "role_name": "", "description": ""}


def evidence_ids(transcript) -> list[str]:
    """Evidence ids from the source tool results so far, as an agent would cite them."""
    out = []
    for t in transcript:
        for r in t.get("results") or [] if t["role"] == "tool" else []:
            if not r["is_error"] and '"evidence_id"' in r["content"]:
                out.append(json.loads(r["content"])["evidence_id"])
    return out


def design_steps(p, fact=FACT_SALES):
    return [
        {"text": "Looking at the sources.", "tool_calls": [{"name": "list_sources", "input": {}}]},
        lambda t: {"tool_calls": [{"name": "source_describe_table", "input": {
            "source_id": last_result(t)["sources"][0]["source_id"], "table": "order_lines", "purpose": "grain"}}]},
        {"tool_calls": [{"name": "put_table", "input": DIM_STORE}, {"name": "put_table", "input": fact}]},
        {"tool_calls": [{"name": "put_relationships", "input": {"relationships": [REL]}},
                        {"name": "map_requirement", "input": {"requirement_id": p.revenue_req,
                                                              "elements": ["fact_sales.net_amount", "dim_store"],
                                                              "note": ""}}]},
        lambda t: {"tool_calls": [{"name": "record_decision", "input": {
            "kind": "decision", "statement": "Order-line grain for sales", "rationale": "Finest available grain",
            "alternatives": ["order grain"], "affects_ids": [p.revenue_req], "affects_elements": ["fact_sales"],
            "evidence_ids": evidence_ids(t)[:1], "citations": []}},
            {"name": "update_feasibility", "input": {"requirement_id": p.profit_req, "expected_seq": 0,
                                                     "feasibility_status": "unsupported",
                                                     "feasibility_notes": "No cost data at order grain"}}]},
        {"text": "Designed fact_sales at order-line grain with dim_store. Profit by order is unsupported."},
    ]


def _seq_of(p, record_id):
    snap = p.alice.get(f"{p.base}/models/{p.model}/snapshot").json()
    return next(r["seq"] for r in snap["requirements"] if r["id"] == record_id)


def _fix_seq(p, steps):
    """update_feasibility needs the requirement's current seq."""
    seq = _seq_of(p, p.profit_req)
    step = steps[4]

    def fixed(t):
        out = step(t) if callable(step) else step
        out["tool_calls"][1]["input"]["expected_seq"] = seq
        return out

    steps[4] = fixed
    return steps


def test_architect_designs_and_applies_to_an_empty_model(proj, db_session_factory):
    proj.script(*_fix_seq(proj, design_steps(proj)))
    events = proj.run()
    done = events[-1]
    assert done["status"] == "completed", done
    assert done["candidate"]["status"] == "applied" and done["candidate"]["hard_failures"] == 0

    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    tables = {t["data"]["name"]: t for t in snap["tables"]}
    assert set(tables) == {"dim_store", "fact_sales"}
    assert tables["fact_sales"]["data"]["grain"] == "One row per order line"
    assert len(snap["attributes"]) == 5 and len(snap["relationships"]) == 1
    links = {(l["from_id"], l["link_type"]) for l in snap["links"]}
    assert (proj.revenue_req, "maps_to") in links
    assert any(l["link_type"] == "affects" and l["to_id"] == tables["fact_sales"]["id"] for l in snap["links"])
    profit = next(r for r in snap["requirements"] if r["id"] == proj.profit_req)
    assert profit["data"]["feasibility_status"] == "unsupported"
    assert len(snap["decisions"]) == 1

    with db_session_factory() as s:
        # The whole design landed as one changeset attributed to the agent run.
        cs = s.scalar(select(Changeset).where(Changeset.reason.like("Applied design candidate%")))
        assert cs.actor_kind == ActorKind.AGENT and cs.model_id == uuid.UUID(proj.model)
        assert s.scalar(select(DesignCandidate)).applied_seq == cs.seq
        ev = s.scalar(select(SourceEvidence).where(SourceEvidence.tool == "describe_table"))
        assert ev.actor_kind == ActorKind.AGENT and ev.purpose == "grain"
    reply = proj.alice.get(f"{proj.base}/conversations/{proj.conv['id']}/messages").json()[-1]["text"]
    assert "Applied to the model as changeset" in reply


def test_partial_draft_with_failed_hard_checks_is_applied_but_flagged(proj):
    no_grain = {**FACT_SALES, "grain": ""}
    proj.script(*_fix_seq(proj, design_steps(proj, fact=no_grain)))
    done = proj.run()[-1]
    assert done["candidate"]["status"] == "applied" and done["candidate"]["hard_failures"] == 1
    cand = proj.alice.get(f"{proj.base}/candidates/{done['candidate']['id']}").json()
    grain = next(c for c in cand["internal_checks"] if c["name"] == "fact_grain_declared")
    assert grain["status"] == "failed" and grain["elements"] == ["fact_sales"]
    reply = proj.alice.get(f"{proj.base}/conversations/{proj.conv['id']}/messages").json()[-1]["text"]
    assert "stays a **Draft**" in reply
    state = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()["state"]
    assert state["effective_state"] == "draft"


def test_non_empty_model_gets_a_proposal_the_user_applies(proj):
    # The user already modeled the store dimension.
    made = proj.alice.post(f"{proj.base}/changesets", json={"model_id": proj.model, "operations": [
        {"op": "create", "type": "table", "ref": "s", "data": {"name": "dim_store", "kind": "dimension"}},
        {"op": "create", "type": "attribute", "data": {"table_id": "$s", "name": "store_id", "logical_type": "text",
                                                        "role": "natural_key"}},
    ]}).json()
    store_id = made["created"]["s"]
    proj.script(*_fix_seq(proj, design_steps(proj)))
    done = proj.run()[-1]
    assert done["candidate"]["status"] == "proposed"
    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    assert [t["data"]["name"] for t in snap["tables"]] == ["dim_store"]  # live model untouched

    cand = proj.alice.get(f"{proj.base}/candidates/{done['candidate']['id']}").json()
    assert not cand["stale"]
    assert "fact fact_sales" in cand["diff"]["added"] and "table dim_store" in cand["diff"]["changed"]
    assert "attribute dim_store.store_key" in cand["diff"]["added"]

    applied = proj.alice.post(f"{proj.base}/candidates/{cand['id']}/apply").json()
    assert applied["status"] == "applied" and applied["diff"] is None
    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    store = next(t for t in snap["tables"] if t["data"]["name"] == "dim_store")
    assert store["id"] == store_id  # updated in place: identity and history kept
    assert store["data"]["scd_type"] == "type_1"
    assert len([a for a in snap["attributes"] if a["data"]["table_id"] == store_id]) == 3


def test_stale_candidate_is_refused(proj):
    proj.alice.post(f"{proj.base}/changesets", json={"model_id": proj.model, "operations": [
        {"op": "create", "type": "table", "data": {"name": "dim_date", "kind": "dimension"}}]})
    proj.script(*_fix_seq(proj, design_steps(proj)))
    done = proj.run()[-1]
    # The user edits the model after the Architect read it.
    proj.alice.post(f"{proj.base}/changesets", json={"model_id": proj.model, "operations": [
        {"op": "create", "type": "table", "data": {"name": "dim_product", "kind": "dimension"}}]})
    cid = done["candidate"]["id"]
    assert proj.alice.get(f"{proj.base}/candidates/{cid}").json()["stale"] is True
    resp = proj.alice.post(f"{proj.base}/candidates/{cid}/apply")
    assert resp.status_code == 409 and "changed after" in resp.json()["detail"]
    names = {t["data"]["name"] for t in proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()["tables"]}
    assert names == {"dim_date", "dim_product"}


def test_a_new_design_supersedes_unapplied_candidates(proj):
    proj.alice.post(f"{proj.base}/changesets", json={"model_id": proj.model, "operations": [
        {"op": "create", "type": "table", "data": {"name": "dim_date", "kind": "dimension"}}]})
    proj.script(*_fix_seq(proj, design_steps(proj)))
    first = proj.run()[-1]["candidate"]["id"]
    proj.script({"tool_calls": [{"name": "put_table", "input": DIM_STORE}]}, {"text": "Revised."})
    proj.run("Try again")
    listed = proj.alice.get(f"{proj.base}/models/{proj.model}/candidates").json()
    assert [c["status"] for c in listed] == ["proposed", "discarded"] and listed[1]["id"] == first


def test_source_tools_only_reach_linked_sources(proj):
    other = proj.alice.post(f"{proj.base}/sources", json={"name": "other", "host": "127.0.0.1", "port": 3306,
                                                          "database": "x", "username": "ro", "password": "x"}).json()
    model = proj.script(
        {"tool_calls": [{"name": "source_list_tables", "input": {"source_id": other["id"], "purpose": ""}}]},
        {"tool_calls": [{"name": "source_describe_table", "input": {"source_id": proj.source, "table": "missing",
                                                                    "purpose": ""}}]},
        {"text": "Done."},
    )
    done = proj.run()[-1]
    results = [t["results"][0] for t in model.calls[-1]["transcript"] if t["role"] == "tool"]
    assert results[0]["is_error"] and "isn't linked" in results[0]["content"]
    assert results[1]["is_error"] and "unknown table" in results[1]["content"] and "evidence" in results[1]["content"]
    assert done["candidate"]["status"] == "failed"  # nothing designed, nothing applied


def test_candidate_tools_validate_input(proj):
    bad_rel = {**REL, "to_table": "dim_nowhere"}
    model = proj.script(
        {"tool_calls": [{"name": "put_table", "input": {**FACT_SALES, "kind": "cube"}}]},
        {"tool_calls": [{"name": "put_table", "input": FACT_SALES},
                        {"name": "put_relationships", "input": {"relationships": [bad_rel]}},
                        {"name": "map_requirement", "input": {"requirement_id": proj.revenue_req,
                                                              "elements": ["fact_sales.nope"], "note": ""}}]},
        {"text": "Done."},
    )
    proj.run()
    results = [r for t in model.calls[-1]["transcript"] if t["role"] == "tool" for r in t["results"]]
    assert results[0]["is_error"] and "cube" in results[0]["content"]
    assert not results[1]["is_error"]
    assert results[2]["is_error"] and "dim_nowhere" in results[2]["content"]
    assert results[3]["is_error"] and "no attribute nope" in results[3]["content"]


def test_internal_checks():
    req = modeling.RecordView(uuid.uuid4(), modeling.RecordType.REQUIREMENT, "REQ-001", None, 1, 1,
                              {"statement": "x", "feasibility_status": "not_assessed", "confirmation_status": "unconfirmed"})
    unsupported = modeling.RecordView(uuid.uuid4(), modeling.RecordType.REQUIREMENT, "REQ-002", None, 1, 1,
                                      {"statement": "y", "feasibility_status": "unsupported"})
    content = {"tables": [
        {"name": "fact_sales", "kind": "fact", "grain": None, "source_ref": None, "attributes": [
            {"name": "qty", "logical_type": "text", "role": "measure", "additivity": None}]},
        {"name": "dim_store", "kind": "dimension", "grain": None, "source_ref": "stores", "attributes": [
            {"name": "name", "logical_type": "text", "role": "descriptive"},
            {"name": "amount", "logical_type": "decimal", "role": "measure", "additivity": "additive"}]},
    ], "relationships": [], "mappings": [], "decision_links": [], "removed": []}
    checks = {c["name"]: c for c in design.internal_checks(content, [req, unsupported])}
    assert checks["fact_grain_declared"]["status"] == "failed"
    assert checks["dimension_keys"]["elements"] == ["dim_store"]
    assert set(checks["measures"]["elements"]) == {"dim_store.amount", "fact_sales.qty"}
    assert checks["numeric_measures"]["hard"] is False
    assert checks["facts_join_dimensions"]["status"] == "failed"
    assert checks["requirements_mapped"]["elements"] == ["REQ-001"]  # unsupported ones are exempt
    assert checks["source_evidence"]["status"] == "unverified"  # missing evidence is never a pass
    assert design.internal_checks({**content, "tables": []}, [])[0]["status"] == "failed"


def test_agents_may_revise_feasibility_but_not_meaning_of_confirmed_requirements(proj, db_session_factory):
    seq = _seq_of(proj, proj.revenue_req)
    proj.alice.post(f"{proj.base}/changesets", json={"operations": [
        {"op": "update", "id": proj.revenue_req, "expected_seq": seq, "data": {"confirmation_status": "confirmed"}}]})
    seq = _seq_of(proj, proj.revenue_req)
    with db_session_factory() as s:
        alice_id = s.scalar(select(Changeset.actor_id).limit(1))
        tenant_id, project_id = (uuid.UUID(x) for x in proj.base.split("/")[2::2])
        agent = Actor(kind=ActorKind.AGENT, id=uuid.uuid4(), on_behalf_of=alice_id)
        scope = tenancy.resolve_project_scope(s, agent, tenant_id, project_id)
        modeling.apply_changeset(s, scope, [UpdateOp(id=uuid.UUID(proj.revenue_req), expected_seq=seq, data={
            "feasibility_status": "fully_supported", "feasibility_notes": "order_lines has net amounts"})])
        new_seq = modeling.current_seq(s, scope)
        with pytest.raises(InvalidOperation, match="confirmed by a person"):
            modeling.apply_changeset(s, scope, [UpdateOp(id=uuid.UUID(proj.revenue_req), expected_seq=new_seq,
                                                         data={"statement": "Something else"})])


def test_architect_conversations_need_a_model(proj):
    resp = proj.alice.post(f"{proj.base}/conversations", json={"agent": "architect"})
    assert resp.status_code == 409 and "data model" in resp.json()["detail"]
    assert proj.conv["title"] == "Architect"


def test_candidates_are_isolated_between_tenants(proj, as_user):
    proj.script(*_fix_seq(proj, design_steps(proj)))
    cid = proj.run()[-1]["candidate"]["id"]
    # Bob is a member of his own tenant and project, and also of Alice's tenant (not the project).
    bob = as_user("auth0|bob")
    tenant_b = bob.post("/tenants", json={"name": "Other"}).json()["id"]
    project_b = bob.post(f"/tenants/{tenant_b}/projects", json={"name": "P"}).json()["id"]
    tenant_a = proj.base.split("/")[2]
    proj.alice.put(f"/tenants/{tenant_a}/members/{bob.id}", json={"role": "member"})
    for base in (f"/tenants/{tenant_b}/projects/{project_b}", proj.base):
        assert bob.get(f"{base}/candidates/{cid}").status_code == 404
        assert bob.post(f"{base}/candidates/{cid}/apply").status_code == 404


# --- ask_requirements: the Architect consults the Business Requirements subgraph ------------

NET_REVENUE = {
    "term": "Net revenue", "aliases": [], "kind": "metric", "definition": "Gross sales less discounts and refunds",
    "business_scope": "", "metric": {"formula": "gross - discounts - refunds", "inclusions": "", "exclusions": "",
                                     "grain": "", "time_basis": "", "aggregation": ""},
    "provenance": "documented", "origin_ref": "Finance glossary v1 · page 2", "conflicts_with_ids": [],
}


def _ask(p, question="How is net revenue defined?"):
    return {"name": "ask_requirements", "input": {
        "question": question, "why": "Which measures fact_sales needs", "concerns_ids": [p.revenue_req],
        "evidence_ids": []}}


def _tool_results(transcript) -> list[dict]:
    out = []
    for t in transcript:
        if t["role"] == "tool":
            for r in t["results"]:
                out.append({"is_error": r["is_error"], "content": r["content"] if r["is_error"] else json.loads(r["content"])})
    return out


GLOSSARY = b"""# Finance glossary

Net revenue is gross sales less discounts and refunds, reported in the month of the order.
"""


def _upload_glossary(p) -> None:
    r = p.alice.post(f"{p.base}/documents", files={"file": ("finance-glossary.md", GLOSSARY, "text/markdown")})
    assert r.status_code == 201, r.text


def test_architect_consults_requirements_and_gets_a_documented_answer(proj, db_session_factory):
    _upload_glossary(proj)
    seen = {}

    def capture(t):
        seen["result"] = last_result(t)
        return {"text": "Noted the definition."}

    architect, requirements, _ = proj.script_agents(
        [{"tool_calls": [_ask(proj)]}, capture],
        [{"tool_calls": [{"name": "get_registers", "input": {}}]},
         {"tool_calls": [{"name": "search_documents", "input": {"query": "net revenue", "limit": 3}}]},
         {"tool_calls": [{"name": "record_definition", "input": NET_REVENUE}]},
         lambda t: {"tool_calls": [{"name": "answer_architect", "input": {
             "outcome": "answered_from_evidence", "answer": "Net revenue is gross less discounts and refunds.",
             "citations": ["Finance glossary v1 · page 2"], "record_ids": [last_result(t)["id"]],
             "needs_user": False}}]}],
    )
    events = proj.run()
    assert events[-1]["status"] == "completed", events[-1]
    answer = seen["result"]
    assert answer["outcome"] == "answered_from_evidence" and answer["needs_user"] is False
    assert answer["citations"] == ["Finance glossary v1 · page 2"]
    [record] = answer["records"]
    assert record["type"] == "definition" and record["confirmation_status"] == "unconfirmed"
    # The handoff carried a brief (question, concerned records), not the Architect's transcript.
    brief = requirements.calls[0]["transcript"]
    assert len(brief) == 1 and "<architect_brief>" in brief[0]["text"] and proj.revenue_req in brief[0]["text"]
    assert "Design the model" not in brief[0]["text"]
    assert "answer_architect" in requirements.calls[0]["tools"]
    assert "record_requirement" not in requirements.calls[0]["tools"]
    # Its tool activity is streamed, tagged with the agent; its text is not.
    assert any(e.get("type") == "tool" and e.get("agent") == "business_requirements" for e in events)

    with db_session_factory() as s:
        child = s.scalar(select(AgentRun).where(AgentRun.parent_run_id.is_not(None)))
        assert child.agent == "business_requirements" and child.status == "completed"
        assert child.iterations == 4 and child.input_tokens == 400  # registers, search, record, answer
        # The definition is attributed to the consultation run, on behalf of the user.
        cs = s.scalar(select(Changeset).where(Changeset.seq == child.changeset_seqs[0]))
        assert cs.actor_kind == ActorKind.AGENT and cs.actor_id == child.id
        parent_id = child.parent_run_id
    run = proj.alice.get(f"{proj.base}/agent-runs/{parent_id}").json()
    assert run["child_run_ids"] == [str(child.id)] and run["parent_run_id"] is None
    assert proj.alice.get(f"{proj.base}/agent-runs/{child.id}").json()["parent_run_id"] == str(parent_id)


def test_consultations_are_bounded_and_cannot_fake_authority(proj, monkeypatch):
    monkeypatch.setattr(get_settings(), "architect_max_consultations", 2)
    _upload_glossary(proj)
    seen = {}

    def finish(t):
        seen["results"] = _tool_results(t)
        return {"text": "Done."}

    proj.script_agents(
        [{"tool_calls": [_ask(proj)]},
         {"tool_calls": [_ask(proj, "  how is NET revenue   defined? ")]},  # the same question again
         {"tool_calls": [_ask(proj, "Do returns count in the refund month?")]},
         {"tool_calls": [_ask(proj, "A third question")]},  # over the limit
         finish],
        [
            # First consultation: no user spoke, so it can't claim user_stated; an inferred
            # definition isn't an answer from evidence, and neither is one without citations;
            # it ends by raising a question.
            {"tool_calls": [{"name": "record_definition", "input": {**NET_REVENUE, "provenance": "user_stated"}}]},
            {"tool_calls": [{"name": "record_definition", "input": {**NET_REVENUE, "provenance": "inferred"}}]},
            lambda t: {"tool_calls": [{"name": "answer_architect", "input": {
                "outcome": "answered_from_evidence", "answer": "Probably gross less discounts.",
                "citations": ["Finance glossary v1 · page 2"], "record_ids": [last_result(t)["id"]],
                "needs_user": False}}]},
            {"tool_calls": [{"name": "record_definition", "input": NET_REVENUE}]},
            lambda t: {"tool_calls": [{"name": "answer_architect", "input": {
                "outcome": "answered_from_evidence", "answer": "Gross less discounts.", "citations": [],
                "record_ids": [last_result(t)["id"]], "needs_user": False}}]},
            {"tool_calls": [{"name": "raise_question", "input": {
                "kind": "clarification", "description": "Which deductions does net revenue exclude?", "impact": "",
                "is_blocking": False, "proposed_action": "Gross less discounts and refunds", "concerns_ids": []}}]},
            lambda t: {"tool_calls": [{"name": "answer_architect", "input": {
                "outcome": "new_question", "answer": "The documents don't define it.", "citations": [],
                "record_ids": [last_result(t)["id"]], "needs_user": False}}]},
            # Second consultation ends without a structured answer.
            {"text": "I couldn't find anything about return timing."},
        ],
    )
    assert proj.run()[-1]["status"] == "completed"
    first, repeat, second, over = seen["results"]
    assert first["content"]["outcome"] == "new_question" and first["content"]["needs_user"] is True
    assert first["content"]["records"][0]["type"] == "question"
    assert repeat["is_error"] and "already asked" in repeat["content"]
    assert second["content"]["outcome"] == "no_answer" and "return timing" in second["content"]["answer"]
    assert over["is_error"] and "all 2 consultations" in over["content"]

    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    # The user_stated attempt was refused; the inferred and documented ones were recorded.
    assert sorted(d["data"]["provenance"] for d in snap["definitions"]) == ["documented", "inferred"]
    assert all(d["data"]["confirmation_status"] == "unconfirmed" for d in snap["definitions"])
    assert [q["data"]["description"] for q in snap["questions"]] == ["Which deductions does net revenue exclude?"]


def test_without_a_requirements_model_the_architect_is_told_to_raise_questions(proj):
    seen = {}

    def capture(t):
        seen["r"] = [t for t in t if t["role"] == "tool"][-1]["results"][0]
        return {"text": "ok"}

    def factory(agent):
        if agent == "business_requirements":
            from app.adapters.llm import LLMError
            raise LLMError("not configured")
        return m

    m = ScriptedChatModel([{"tool_calls": [_ask(proj)]}, capture])
    proj.client.app.state.chat_model_factory = factory
    proj.run()
    assert seen["r"]["is_error"] and "raise_question" in seen["r"]["content"]
