"""Describe with AI: one model call fills in the New table form. It writes nothing to the model;
the call is recorded as a run with its transcript and usage."""

import uuid

import pytest
from sqlalchemy import select

from app.adapters.llm import LLMError, ScriptedChatModel
from app.agents.table_suggestion.agent import SUGGEST_SPEC
from app.persistence.conversations import AgentRun, RunMessage


def suggestion(**overrides):
    table = {
        "name": "dim_vendor", "kind": "dimension", "grain": None, "description": "One row per vendor.",
        "scd_type": "none", "notes": None,
        "attributes": [
            {"name": "vendor_sk", "logical_type": "integer", "role": "surrogate_key", "is_nullable": False,
             "additivity": None, "description": "Surrogate key."},
            {"name": "vendor_id", "logical_type": "text", "role": "natural_key", "is_nullable": False,
             "additivity": "additive", "description": "The vendor's id in the source."},
            {"name": "vendor_name", "logical_type": "text", "role": "descriptive", "is_nullable": True,
             "additivity": "none", "description": "The vendor's name."},
        ],
    }
    return {"tool_calls": [{"name": "suggest_table", "input": {**table, **overrides}}]}


@pytest.fixture
def proj(as_user, client):
    alice = as_user("auth0|alice")
    tenant = alice.post("/tenants", json={"name": "Northstar"}).json()["id"]
    project = alice.post(f"/tenants/{tenant}/projects", json={"name": "Discovery"}).json()["id"]
    base = f"/tenants/{tenant}/projects/{project}"
    model = alice.post(f"{base}/models", json={"name": "Retail"}).json()["id"]
    alice.post(f"{base}/changesets", json={"model_id": model, "operations": [
        {"op": "create", "type": "table", "ref": "s", "data": {"name": "dim_store", "kind": "dimension"}},
        {"op": "create", "type": "attribute", "data": {"table_id": "$s", "name": "store_sk",
                                                       "logical_type": "integer", "role": "surrogate_key"}},
    ]}).raise_for_status()

    class P:
        pass

    p = P()
    p.alice, p.tenant, p.base, p.model, p.client = alice, tenant, base, model, client
    p.url = f"{base}/models/{model}/table-suggestions"

    def use(*steps):
        chat = ScriptedChatModel(list(steps), model="scripted-suggest")
        client.app.state.chat_model_factory = lambda agent: chat if agent == "table_suggestion" else None
        return chat

    p.use = use
    return p


def test_a_description_fills_in_the_form_and_writes_nothing(proj, db_session_factory):
    chat = proj.use(suggestion())
    before = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    r = proj.alice.post(proj.url, json={"description": "Create a dim_vendor table with sk, natural id and name"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["table"] == {"name": "dim_vendor", "kind": "dimension", "grain": None,
                             "description": "One row per vendor.", "scd_type": "none"}
    assert [(a["name"], a["role"], a["logical_type"]) for a in body["attributes"]] == [
        ("vendor_sk", "surrogate_key", "integer"), ("vendor_id", "natural_key", "text"),
        ("vendor_name", "descriptive", "text")]
    assert body["attributes"][1]["additivity"] is None  # additivity is for measures only

    # Nothing changed in the model.
    after = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    assert after["current_seq"] == before["current_seq"]

    # The model saw the description and the model's tables with their keys, nothing else.
    [call] = chat.calls
    brief = call["transcript"][0]["text"]
    assert "- dim_store (dimension): keys store_sk (surrogate_key, integer)" in brief
    assert "<request>\nCreate a dim_vendor table with sk, natural id and name\n</request>" in brief
    assert call["tools"] == [SUGGEST_SPEC.name]

    # It's a run, attributed to Alice, with its transcript.
    with db_session_factory() as s:
        run = s.get(AgentRun, uuid.UUID(body["run_id"]))
        assert run.agent == "table_suggestion" and run.status.value == "completed" and run.iterations == 1
        assert run.llm_model == "scripted-suggest"
        roles = [m.role for m in s.scalars(select(RunMessage).where(RunMessage.run_id == run.id))]
        assert roles[0] == "system" and "assistant" in roles


def test_a_fact_keeps_its_grain_and_a_name_clash_is_noted(proj):
    proj.use(suggestion(name="dim_store", notes="Added a surrogate key."))
    body = proj.alice.post(proj.url, json={"description": "a store dimension"}).json()
    assert body["notes"] == "Added a surrogate key. A table named dim_store already exists in this model."

    proj.use(suggestion(name="fact_sales", kind="fact", grain="One row per order line.", attributes=[]))
    body = proj.alice.post(proj.url, json={"description": "sales by order line"}).json()
    assert body["table"]["grain"] == "One row per order line." and body["attributes"] == []

    proj.use(suggestion(grain="One row per vendor."))  # a dimension has no grain field
    assert proj.alice.post(proj.url, json={"description": "vendors"}).json()["table"]["grain"] is None


def test_no_valid_suggestion_is_503_and_the_run_failed(proj, db_session_factory):
    proj.use({"text": "I'd rather chat."})
    r = proj.alice.post(proj.url, json={"description": "vendors"})
    assert r.status_code == 503 and "didn't suggest a table" in r.json()["detail"]

    proj.use(suggestion(kind="snowflake"))
    r = proj.alice.post(proj.url, json={"description": "vendors"})
    assert r.status_code == 503 and "wasn't a valid table" in r.json()["detail"]
    with db_session_factory() as s:
        runs = list(s.scalars(select(AgentRun).where(AgentRun.agent == "table_suggestion")))
        assert [r.status.value for r in runs] == ["failed", "failed"]


def test_no_configured_model_is_503(proj):
    def factory(agent):
        raise LLMError(f"no model is configured for the {agent} agent")

    proj.client.app.state.chat_model_factory = factory
    r = proj.alice.post(proj.url, json={"description": "vendors"})
    assert r.status_code == 503 and "isn't available" in r.json()["detail"]


def test_viewers_cant_and_credentials_are_redacted(proj, as_user):
    viewer = as_user("auth0|vi")
    proj.alice.put(f"/tenants/{proj.tenant}/members/{viewer.id}", json={"role": "member"})
    proj.alice.put(f"{proj.base}/members/{viewer.id}", json={"role": "viewer"})
    proj.use(suggestion())
    assert viewer.post(proj.url, json={"description": "vendors"}).status_code == 403

    chat = proj.use(suggestion())
    proj.alice.post(proj.url, json={"description": "vendors, password=hunter2secret"}).raise_for_status()
    assert "hunter2secret" not in chat.calls[0]["transcript"][0]["text"]
    assert proj.alice.post(proj.url, json={"description": "   "}).status_code == 409


def test_the_tool_schema_has_no_nullable_enums():
    """Anthropic's strict tools refuse an enum whose type allows null (400 "does not match
    declared type"), which the scripted model can't show."""
    def walk(node):
        if isinstance(node, dict):
            if "enum" in node:
                assert node.get("type") == "string" and None not in node["enum"], node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(SUGGEST_SPEC.input_schema)


def test_the_dialog_saves_a_suggestion_as_one_user_changeset(proj, db_session_factory):
    """What the New table dialog sends after Fill in: the table and its attributes in one
    changeset, attributed to the user, so the Validator treats them as a person's edit."""
    proj.use(suggestion())
    body = proj.alice.post(proj.url, json={"description": "a vendor dimension"}).json()
    ops = [{"op": "create", "type": "table", "ref": "t", "data": body["table"]}] + [
        {"op": "create", "type": "attribute", "data": {**a, "table_id": "$t", "ordinal": i}}
        for i, a in enumerate(body["attributes"])]
    r = proj.alice.post(f"{proj.base}/changesets", json={"model_id": proj.model, "operations": ops})
    assert r.status_code == 201, r.text
    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    vendor = next(t for t in snap["tables"] if t["data"]["name"] == "dim_vendor")
    names = [a["data"]["name"] for a in sorted(snap["attributes"], key=lambda a: a["data"]["ordinal"])
             if a["data"]["table_id"] == vendor["id"]]
    assert names == ["vendor_sk", "vendor_id", "vendor_name"]
