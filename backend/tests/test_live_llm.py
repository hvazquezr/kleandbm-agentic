"""Live checks against the real Claude and OpenAI APIs. Opt-in (they cost money):
pytest -m live_llm. Requires KLEANDBM_ANTHROPIC_API_KEY; the Validator checks need
KLEANDBM_OPENAI_API_KEY with access to the configured Validator model."""

import pytest

from app.adapters.llm import AnthropicChatModel, OpenAIChatModel, ToolSpec
from app.config import get_settings

pytestmark = pytest.mark.live_llm


@pytest.fixture
def model():
    s = get_settings()
    if not s.anthropic_api_key:
        pytest.skip("KLEANDBM_ANTHROPIC_API_KEY not set")
    return AnthropicChatModel(s.anthropic_api_key, s.requirements_agent_model, "low", 2000)


def test_tool_call_round_trip(model):
    tool = ToolSpec("record_term", "Record a business term.", {
        "type": "object", "properties": {"term": {"type": "string"}},
        "required": ["term"], "additionalProperties": False})
    streamed: list[str] = []
    transcript = [{"role": "user", "text": "Record the business term 'net revenue' using the record_term tool."}]
    turn = model.complete("You record business terms with tools.", transcript, [tool], on_text=streamed.append)
    assert turn.tool_calls and turn.tool_calls[0].name == "record_term"
    assert "net revenue" in turn.tool_calls[0].input["term"].lower()
    assert turn.usage.input_tokens > 0

    # The raw assistant content (thinking included) replays unchanged with the tool result.
    transcript += [turn.to_transcript(),
                   {"role": "tool", "results": [{"id": turn.tool_calls[0].id, "content": '{"code": "DEF-001"}', "is_error": False}]}]
    final = model.complete("You record business terms with tools.", transcript, [tool], on_text=streamed.append)
    assert final.stop_reason == "end_turn" and final.text


def test_agent_tool_definitions_are_accepted(model):
    """Send the agent's real tools: the API validates every schema on the request."""
    from app.agents.requirements.prompt import SYSTEM_PROMPT
    from app.agents.requirements.tools import TOOL_SPECS

    turn = model.complete(SYSTEM_PROMPT, [{"role": "user", "text": "List the project's documents."}], TOOL_SPECS)
    assert turn.stop_reason in {"tool_use", "end_turn"}


def test_vision_transcribes_a_report_screenshot():
    """The Northstar Sales Report capture should be read cell by cell, with exact figures."""
    from pathlib import Path

    from app.adapters.vision import AnthropicImageDescriber

    s = get_settings()
    if not s.anthropic_api_key:
        pytest.skip("KLEANDBM_ANTHROPIC_API_KEY not set")
    image = Path(__file__).resolve().parents[2] / "retail-discovery" / "Sales-Report.png"
    if not image.exists():
        pytest.skip("retail-discovery/Sales-Report.png not present")
    text = AnthropicImageDescriber(s.anthropic_api_key, s.image_description_model, "medium").describe(
        image.read_bytes(), image.name)
    for figure in ["1,001,700", "937,602", "38,976", "577,800", "Downtown", "Riverside"]:
        assert figure in text, figure
    assert "Profit by order" in text


def test_architect_tool_definitions_are_accepted(model):
    from app.agents.architect.prompt import SYSTEM_PROMPT
    from app.agents.architect.tools import TOOL_SPECS

    turn = model.complete(SYSTEM_PROMPT, [{"role": "user", "text": "List the linked sources."}], TOOL_SPECS)
    assert turn.stop_reason in {"tool_use", "end_turn"}


def test_consultation_tool_definitions_are_accepted(model):
    from app.agents.requirements.consult import CONSULT_PROMPT, TOOL_SPECS

    brief = "<architect_brief>\nQuestion: How is net revenue defined?\n</architect_brief>"
    turn = model.complete(CONSULT_PROMPT, [{"role": "user", "text": brief}], TOOL_SPECS)
    assert turn.stop_reason in {"tool_use", "end_turn"}


def test_search_tool_definitions_are_accepted(model):
    """The decision-point generator's strict, nested schema and the evaluator's tools."""
    from app.agents.architect.search import EVAL_TOOLS, EVALUATOR_PROMPT, GENERATOR_PROMPT, PROPOSE_SPEC

    brief = "<decision_brief>\n{\"decision_point\": \"Profit with costs only by store and day\"}\n</decision_brief>"
    turn = model.complete(GENERATOR_PROMPT, [{"role": "user", "text": brief}], [PROPOSE_SPEC])
    assert turn.stop_reason in {"tool_use", "end_turn", "max_tokens"}
    turn = model.complete(EVALUATOR_PROMPT, [{"role": "user", "text": "<evaluation_brief>{}</evaluation_brief>"}], EVAL_TOOLS)
    assert turn.stop_reason in {"tool_use", "end_turn"}


@pytest.fixture
def validator_model():
    s = get_settings()
    if not s.openai_api_key:
        pytest.skip("KLEANDBM_OPENAI_API_KEY not set")
    return OpenAIChatModel(s.openai_api_key, s.validator_agent_model, "low", 4000)


def test_validator_round_trip_and_tool_definitions(validator_model):
    """The Validator's real tools are accepted, a strict call comes back, and the output items
    (reasoning included) replay with the tool result."""
    from app.agents.validator.prompt import SYSTEM_PROMPT
    from app.agents.validator.tools import TOOL_SPECS

    transcript = [{"role": "user", "text": "<review_brief>\nCall get_candidate to start the review.\n</review_brief>"}]
    turn = validator_model.complete(SYSTEM_PROMPT, transcript, TOOL_SPECS)
    assert turn.tool_calls and turn.tool_calls[0].name == "get_candidate", turn
    assert turn.usage.input_tokens > 0
    transcript += [turn.to_transcript(), {"role": "tool", "results": [
        {"id": turn.tool_calls[0].id, "content": '{"tables": [], "relationships": []}', "is_error": False}]}]
    again = validator_model.complete(SYSTEM_PROMPT, transcript, TOOL_SPECS)
    assert again.stop_reason in {"tool_use", "end_turn"}


def test_architect_designs_northstar_from_the_real_source(as_user, client, monkeypatch):
    """End to end on the real Northstar MySQL source: investigate, design, apply. Prints what it
    built for review; asserts only the invariants (applied, grains declared, evidence cited)."""
    import json
    from pathlib import Path

    from cryptography.fernet import Fernet

    from app.adapters.llm import AnthropicChatModel
    from app.adapters.sources import StdioMySqlConnector
    from tests.test_sources import NORTHSTAR_PASSWORD

    s = get_settings()
    if not s.anthropic_api_key or not NORTHSTAR_PASSWORD:
        pytest.skip("needs KLEANDBM_ANTHROPIC_API_KEY and the Northstar MySQL source")
    monkeypatch.setattr(s, "secrets_key", Fernet.generate_key().decode())
    client.app.state.source_connector = StdioMySqlConnector(
        Path(__file__).resolve().parents[2] / "mcp", 5000, 30.0, 200_000)
    chat = AnthropicChatModel(s.anthropic_api_key, s.architect_agent_model, s.architect_agent_effort, s.agent_max_tokens)
    br = AnthropicChatModel(s.anthropic_api_key, s.requirements_agent_model, s.requirements_agent_effort, s.agent_max_tokens)
    validator = None
    if s.openai_api_key:  # the Validator runs on another model family; without a key it's skipped
        validator = OpenAIChatModel(s.openai_api_key, s.validator_agent_model, s.validator_agent_effort,
                                    s.agent_max_tokens)
    models = {"architect": chat, "business_requirements": br, "validator": validator}

    def factory(agent):
        if models.get(agent) is None:
            from app.adapters.llm import LLMError
            raise LLMError(f"no model is configured for the {agent} agent")
        return models[agent]

    client.app.state.chat_model_factory = factory

    alice = as_user("auth0|alice")
    tenant = alice.post("/tenants", json={"name": "Northstar"}).json()["id"]
    project = alice.post(f"/tenants/{tenant}/projects", json={"name": "Retail"}).json()["id"]
    base = f"/tenants/{tenant}/projects/{project}"
    model = alice.post(f"{base}/models", json={"name": "Q3 Sales"}).json()["id"]
    src = alice.post(f"{base}/sources", json={"name": "Northstar ops", "host": "127.0.0.1", "port": 3306,
                                              "database": "northstar_ops", "username": "northstar_ro",
                                              "password": NORTHSTAR_PASSWORD})
    assert src.status_code == 201, src.text
    alice.put(f"{base}/models/{model}/sources/{src.json()['id']}")
    # The discovery workbook (two departments' net revenue figures) is what a consultation can
    # answer from. Only this file: the README answer key and verification.json never go in.
    workbook = Path(__file__).resolve().parents[2] / "retail-discovery" / "northstar-current-state-report.xlsx"
    doc = alice.post(f"{base}/documents", files={"file": (workbook.name, workbook.read_bytes(),
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert doc.status_code == 201, doc.text
    alice.post(f"{base}/changesets", json={"operations": [
        {"op": "create", "type": "requirement", "data": {"statement": s_, "origin_kind": "user_statement"}}
        for s_ in ["Monthly net revenue by channel, store and product category",
                   "Monthly returns by channel, store and product category",
                   "Profit by order"]]})
    conv = alice.post(f"{base}/conversations", json={"model_id": model, "agent": "architect"}).json()
    resp = alice.post(f"{base}/conversations/{conv['id']}/messages", json={"text": "Design the model."})
    events = [json.loads(l[6:]) for l in resp.text.splitlines() if l.startswith("data: ")]
    done = events[-1]
    tools = [e for e in events if e["type"] == "tool" and e["status"] != "started"]
    alice = as_user("auth0|alice")  # a fresh token: the run outlasts the test token's 5 minutes
    print("\nRUN", json.dumps(done, indent=1))
    print("TOOLS", json.dumps([f"{t['status']}: {t['summary']}" for t in tools], indent=1))

    snap = alice.get(f"{base}/models/{model}/snapshot").json()
    for t in snap["tables"]:
        attrs = [a["data"]["name"] for a in snap["attributes"] if a["data"]["table_id"] == t["id"]]
        print(f"TABLE {t['data']['kind']} {t['data']['name']} grain={t['data']['grain']!r} attrs={attrs}")
    for r in snap["requirements"]:
        print(f"REQ {r['code']} {r['data']['feasibility_status']}: {r['data']['feasibility_notes']}")
    for d in snap["decisions"]:
        print(f"DEC {d['code']} {d['data']['kind']}: {d['data']['statement']}")
    for q in snap["questions"]:
        print(f"Q {q['code']}: {q['data']['description']}")
    for d in snap["definitions"]:
        print(f"DEF {d['code']} {d['data']['provenance']}/{d['data']['confirmation_status']}: "
              f"{d['data']['term']} ({d['data'].get('business_scope')}) — {d['data'].get('origin_ref')}")
    runs = alice.get(f"{base}/agent-runs/{done['run']['id']}").json()
    for cid in runs["child_run_ids"]:
        c = alice.get(f"{base}/agent-runs/{cid}").json()
        print(f"CONSULT {c['agent']} {c['status']} iters={c['iterations']} tools={c['tool_calls']} "
              f"in={c['input_tokens']} out={c['output_tokens']} cache_read={c['cache_read_tokens']}")
    cand = alice.get(f"{base}/candidates/{done['candidate']['id']}").json()
    print("CHECKS", json.dumps([(c["name"], c["status"]) for c in cand["internal_checks"]]))
    for rnd in cand["validation"]:
        print(f"REVIEW round {rnd['round']} {rnd['status']} ({rnd['model']}): {rnd.get('summary') or rnd.get('detail')}")
        for f in rnd["findings"]:
            print(f"  FINDING {f['issue_key']} [{f['check_type']}] {f['status']}{' BLOCKING' if f['is_blocking'] else ''}"
                  f": {f['summary']} | evidence={[e['kind'] for e in f['evidence']]} | fix={f.get('recommended_action')}")
    print("ATTEMPTS", cand["issue_attempts"], "READY", cand["ready_for_review"], "BLOCKERS", cand["blockers"])
    for dp in alice.get(f"{base}/candidates/{done['candidate']['id']}/decision-points").json():
        print(f"DECISION POINT [{dp['trigger']}] {dp['status']} ({dp['stop_reason']}): {dp['question']}")
        print(f"  scope={dp['scope'].get('elements')} stages={dp['scope'].get('stages')} usage={dp['usage']}")
        for n in dp["nodes"]:
            e = n["evaluation"]
            print(f"  {'  ' * n['depth']}NODE d{n['depth']} {n['stage'] or ''} {n['status']}: {n['choice']} "
                  f"| total={e.get('total')} scores={e.get('scores')} hard={e.get('hard_violation')} "
                  f"stage_fail={e.get('stage_hard_failures')} unverified={e.get('unverified_critical')} "
                  f"| tables={n['tables']} | {n['reason']}")
            if e.get("assessment"):
                print(f"  {'  ' * n['depth']}  assessment: {e['assessment']}")
    print("STATE", snap["state"])
    print("REPLY", alice.get(f"{base}/conversations/{conv['id']}/messages").json()[-1]["text"])

    assert done["status"] in {"completed", "stopped"}
    assert done["candidate"]["status"] == "applied"
    facts = [t for t in snap["tables"] if t["data"]["kind"] == "fact"]
    assert facts and all(t["data"]["grain"] for t in facts)
    assert any(t["status"] == "ok" and t["summary"].startswith("Source:") for t in tools)
