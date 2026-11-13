"""The Business Requirements agent: tool use, register writes with provenance, authority
limits, budgets, failures, streaming, and isolation. Uses a scripted model (no API key)."""

import json
import uuid

import pytest

from app.adapters.llm import LLMError, ScriptedChatModel
from app.config import get_settings
from app.domain import modeling, tenancy
from app.domain.access import Actor, ActorKind
from app.domain.modeling import CreateOp, UpdateOp
from app.domain.tenancy import InvalidOperation

GLOSSARY = (
    b"Sales net revenue = gross sales less discounts. Returns are reported separately.\n\n"
    b"Finance net revenue deducts both discounts and returns, attributed to original order month.\n\n"
    b"Profit by order: profit/cost rules not agreed; order and cost detail absent."
)


def sse(resp) -> list[dict]:
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def tool_result(transcript, index: int = -1) -> dict:
    """Parsed content of a tool result in the transcript (default: the latest)."""
    results = [t for t in transcript if t["role"] == "tool"]
    return json.loads(results[index]["results"][0]["content"])


@pytest.fixture
def proj(as_user, client):
    alice = as_user("auth0|alice")
    tenant = alice.post("/tenants", json={"name": "Northstar"}).json()["id"]
    project = alice.post(f"/tenants/{tenant}/projects", json={"name": "Discovery"}).json()["id"]
    base = f"/tenants/{tenant}/projects/{project}"
    model = alice.post(f"{base}/models", json={"name": "Retail"}).json()["id"]
    conv = alice.post(f"{base}/conversations", json={"model_id": model}).json()

    class P:
        pass

    p = P()
    p.alice, p.tenant, p.project, p.base, p.model, p.conv = alice, tenant, project, base, model, conv
    p.client = client

    def script(*steps):
        model_ = ScriptedChatModel(list(steps))
        client.app.state.chat_model_factory = lambda *_: model_
        return model_

    def send(user, text, attachments=()):
        return user.post(f"{base}/conversations/{conv['id']}/messages",
                         json={"text": text, "attachment_version_ids": list(attachments)})

    def upload(name, data):
        return alice.post(f"{base}/documents", files={"file": (name, data, "text/plain")}).json()["latest"]

    p.script, p.send, p.upload = script, send, upload
    return p


def test_agent_reads_documents_records_conflicting_definitions_and_asks(proj, db_session_factory):
    version = proj.upload("northstar-notes.txt", GLOSSARY)
    model = proj.script(
        {"text": "Let me review the documents first.", "tool_calls": [{"name": "list_documents", "input": {}}]},
        lambda t: {"tool_calls": [{"name": "read_document",
                                   "input": {"version_id": tool_result(t)["documents"][0]["version_id"], "start": 0}}]},
        {"tool_calls": [{"name": "record_definition", "input": {
            "term": "Net revenue", "aliases": ["Sales net revenue"], "kind": "metric",
            "definition": "Gross sales less discounts; returns reported separately.",
            "business_scope": "Sales Operations reporting",
            "metric": {"formula": "gross_sales - discounts", "inclusions": "", "exclusions": "returns",
                       "grain": "", "time_basis": "", "aggregation": ""},
            "provenance": "documented", "origin_ref": "northstar-notes.txt v1 · lines 1-1",
            "conflicts_with_ids": []}}]},
        lambda t: {"tool_calls": [{"name": "record_definition", "input": {
            "term": "Net revenue", "aliases": ["Finance net revenue"], "kind": "metric",
            "definition": "Gross sales less discounts and returns, by original order month.",
            "business_scope": "Finance close", "metric": {k: "" for k in ("formula", "inclusions", "exclusions", "grain", "time_basis", "aggregation")},
            "provenance": "documented",
            "origin_ref": "northstar-notes.txt v1 · lines 3-3", "conflicts_with_ids": [tool_result(t)["id"]]}}]},
        lambda t: {"tool_calls": [{"name": "raise_question", "input": {
            "kind": "contradiction", "description": "Sales and Finance define net revenue differently. Which applies?",
            "impact": "Changes every revenue measure.", "is_blocking": False, "proposed_action": "",
            "concerns_ids": [tool_result(t, -2)["id"], tool_result(t)["id"]]}}]},
        {"text": "I recorded DEF-001 and DEF-002 (they conflict) and raised Q-001. Which net revenue should the model use?"},
    )

    resp = proj.send(proj.alice, "Please review the Northstar notes.", [version["id"]])
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    events = sse(resp)
    assert events[0]["type"] == "run"
    assert "".join(e["text"] for e in events if e["type"] == "text").strip().endswith("model use?")
    oks = [e for e in events if e["type"] == "tool" and e["status"] == "ok"]
    assert [e["name"] for e in oks] == ["list_documents", "read_document", "record_definition",
                                         "record_definition", "raise_question"]
    assert oks[2]["record"]["code"] == "DEF-001"
    done = events[-1]
    assert done["type"] == "done" and done["status"] == "completed"
    assert done["run"]["iterations"] == 6 and done["run"]["tool_calls"] == 5

    # The first model call saw the project context, including the attachment and documents.
    first = model.calls[0]["transcript"][-1]["text"]
    assert "<project_context>" in first and "northstar-notes.txt" in first and "Attached to this message" in first
    assert "record_definition" in model.calls[0]["tools"]

    # Registers: both meanings kept, unconfirmed, linked as conflicting, with a question.
    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    defs = {d["code"]: d for d in snap["definitions"]}
    assert set(defs) == {"DEF-001", "DEF-002"}
    assert all(d["data"]["confirmation_status"] == "unconfirmed" and d["data"]["provenance"] == "documented" for d in defs.values())
    assert defs["DEF-001"]["data"]["metric"]["grain"] is None  # unknown details stay unresolved
    link_types = sorted(l["link_type"] for l in snap["links"])
    assert link_types == ["concerns", "concerns", "conflicts_with"]
    assert snap["questions"][0]["data"]["kind"] == "contradiction"

    # Attribution: agent actor on behalf of Alice, traced to the run.
    run = proj.alice.get(f"{proj.base}/agent-runs/{done['run']['id']}").json()
    assert run["status"] == "completed" and len(run["changeset_seqs"]) == 3 and run["input_tokens"] == 600
    history = proj.alice.get(f"{proj.base}/records/{defs['DEF-001']['id']}/history").json()
    assert history[0]["actor_kind"] == "agent" and history[0]["actor_id"] == run["id"]
    assert history[0]["on_behalf_of"] == str(proj.alice.id)

    # Transcript: the user message (with attachment) and the assistant reply with its activity.
    messages = proj.alice.get(f"{proj.base}/conversations/{proj.conv['id']}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["attachments"] == [version["id"]]
    assert [a["tool"] for a in messages[1]["activity"]][-1] == "raise_question"


def test_second_message_includes_history(proj):
    proj.script({"text": "Noted: you care about monthly revenue."})
    proj.send(proj.alice, "We report revenue monthly.")
    model = proj.script({"text": "Understood."})
    proj.send(proj.alice, "Also by store.")
    roles = [(t["role"], t.get("text", "")[:20]) for t in model.calls[0]["transcript"]]
    assert roles[0] == ("user", "We report revenue mo") and roles[1][0] == "assistant"
    assert "Also by store." in model.calls[0]["transcript"][-1]["text"]


def test_tool_errors_go_back_to_the_model(proj):
    model = proj.script(
        {"tool_calls": [{"name": "update_entry", "input": {
            "record_id": str(uuid.uuid4()), "expected_seq": 1, "statement": "x", "definition": "",
            "business_scope": "", "origin_ref": "", "feasibility_status": "unchanged", "feasibility_notes": "",
            "question_status": "unchanged", "resolution": ""}}]},
        {"text": "That record doesn't exist; I'll check the registers next time."},
    )
    events = sse(proj.send(proj.alice, "Update the requirement."))
    errors = [e for e in events if e["type"] == "tool" and e["status"] == "error"]
    assert errors and "unknown record" in errors[0]["summary"]
    assert events[-1]["status"] == "completed"
    tool_turn = [t for t in model.calls[1]["transcript"] if t["role"] == "tool"][0]
    assert tool_turn["results"][0]["is_error"] is True


def test_execution_budget_stops_the_run(proj, monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_max_iterations", 3)
    proj.script(*[{"tool_calls": [{"name": "get_registers", "input": {}}]} for _ in range(10)])
    events = sse(proj.send(proj.alice, "Go."))
    assert events[-1]["status"] == "stopped" and events[-1]["run"]["iterations"] == 3
    reply = proj.alice.get(f"{proj.base}/conversations/{proj.conv['id']}/messages").json()[-1]
    assert "step limit" in reply["text"]


def test_provider_failure_is_recorded_not_raised(proj):
    class Down:
        model = "down-model"

        def complete(self, *a, **kw):
            raise LLMError("could not reach the Anthropic API")

    proj.client.app.state.chat_model_factory = lambda *_: Down()
    events = sse(proj.send(proj.alice, "Hello?"))
    assert events[-1]["status"] == "failed" and "could not reach" in events[-1]["detail"]
    reply = proj.alice.get(f"{proj.base}/conversations/{proj.conv['id']}/messages").json()[-1]
    assert reply["role"] == "assistant" and "couldn't finish" in reply["text"]
    # A new message can be sent after a failed run.
    proj.script({"text": "Back."})
    assert sse(proj.send(proj.alice, "Retry"))[-1]["status"] == "completed"


def test_missing_api_key_fails_before_anything_is_stored(proj):
    def no_key(*_):
        raise LLMError("KLEANDBM_ANTHROPIC_API_KEY is not set; add it to backend/.env")

    proj.client.app.state.chat_model_factory = no_key
    resp = proj.send(proj.alice, "Hi")
    assert resp.status_code == 409 and "ANTHROPIC_API_KEY" in resp.json()["detail"]
    assert proj.alice.get(f"{proj.base}/conversations/{proj.conv['id']}/messages").json() == []


def test_agents_cannot_confirm_or_alter_confirmed_meanings(proj, db_session_factory):
    with db_session_factory() as s:
        user = Actor(kind=ActorKind.USER, id=proj.alice.id)
        agent = Actor(kind=ActorKind.AGENT, id=uuid.uuid4(), on_behalf_of=proj.alice.id)
        tid, pid = uuid.UUID(proj.tenant), uuid.UUID(proj.project)
        user_scope = tenancy.resolve_project_scope(s, user, tid, pid)
        agent_scope = tenancy.resolve_project_scope(s, agent, tid, pid)

        with pytest.raises(InvalidOperation, match="only a person"):
            modeling.apply_changeset(s, agent_scope, [CreateOp(type="definition", data={
                "term": "Active customer", "provenance": "inferred", "confirmation_status": "confirmed"})])
        s.rollback()

        made = modeling.apply_changeset(s, agent_scope, [CreateOp(type="definition", ref="d", data={
            "term": "Active customer", "provenance": "inferred"})])
        d = made.created["d"]
        # A person confirms it ...
        modeling.apply_changeset(s, user_scope, [UpdateOp(id=d, expected_seq=made.seq, data={"confirmation_status": "confirmed"})])
        s.commit()  # each rejected attempt below rolls back, so keep the confirmed state
        seq = modeling.get_record(s, user_scope, d).seq
        # ... and the agent can no longer change it, or un-confirm it.
        for change in ({"definition": "Bought in the last 30 days"}, {"confirmation_status": "unconfirmed"}):
            with pytest.raises(InvalidOperation, match="confirmed by a person"):
                modeling.apply_changeset(s, agent_scope, [UpdateOp(id=d, expected_seq=seq, data=change)])
            s.rollback()
        s.commit()


def test_conversations_are_scoped_and_role_checked(proj, as_user):
    proj.script({"text": "Hi."})
    proj.send(proj.alice, "Hello")

    viewer = as_user("auth0|vi")
    proj.alice.put(f"/tenants/{proj.tenant}/members/{viewer.id}", json={"role": "member"})
    proj.alice.put(f"{proj.base}/members/{viewer.id}", json={"role": "viewer"})
    assert len(viewer.get(f"{proj.base}/conversations/{proj.conv['id']}/messages").json()) == 2
    assert proj.send(viewer, "Let me in").status_code == 403

    bob = as_user("auth0|bob")
    assert bob.get(f"{proj.base}/conversations/{proj.conv['id']}/messages").status_code == 404
    tb = bob.post("/tenants", json={"name": "Globex"}).json()["id"]
    pb = bob.post(f"/tenants/{tb}/projects", json={"name": "Ops"}).json()["id"]
    bob_conv = bob.post(f"/tenants/{tb}/projects/{pb}/conversations", json={}).json()
    # Bob can't attach Alice's document to his own conversation.
    alice_doc = proj.upload("secret.txt", b"Confidential margin rules.")
    resp = bob.post(f"/tenants/{tb}/projects/{pb}/conversations/{bob_conv['id']}/messages",
                    json={"text": "read this", "attachment_version_ids": [alice_doc["id"]]})
    assert resp.status_code == 404
    assert bob.get(f"/tenants/{tb}/projects/{pb}/conversations/{proj.conv['id']}/messages").status_code == 404


def _walk(schema):
    yield schema
    for value in schema.values():
        if isinstance(value, dict):
            yield from _walk(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from _walk(item)


def test_tool_schemas_fit_strict_mode():
    """Strict tool use rejects some JSON Schema keywords (found live: integer minimum/maximum).
    Every object must also close itself and require all its properties."""
    from app.agents.requirements.tools import TOOL_SPECS

    unsupported = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                   "minLength", "maxLength", "pattern", "minItems", "maxItems", "format"}
    for spec in TOOL_SPECS:
        for node in _walk(spec.input_schema):
            assert not unsupported & set(node), f"{spec.name}: unsupported keyword in {node}"
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, spec.name
                assert set(node.get("required", [])) == set(node.get("properties", {})), spec.name
    # Also found live: at most 16 union-typed (nullable / anyOf) parameters per request.
    unions = sum(
        1 for spec in TOOL_SPECS for node in _walk(spec.input_schema)
        if isinstance(node.get("type"), list) or "anyOf" in node
    )
    assert unions <= 16, f"{unions} union-typed parameters; strict mode allows 16"
