"""The Validator: an independent review of the Architect's candidate on another model family,
bounded correction rounds, per-issue attempts carried across episodes, findings saved as CHECK
records, Ready for Review gating, escalation, and person-edited elements. Scripted models and
a fake source."""

import json

from sqlalchemy import select

from app.adapters.llm import LLMError, ScriptedChatModel
from app.config import get_settings
from app.persistence.conversations import AgentRun
from tests.test_architect import FACT_SALES, _fix_seq, design_steps, last_result, proj  # noqa: F401


def finding(key, status="passed", *, blocking=False, elements=(), reqs=(), evidence=(), checks=(), kind="grain",
            action="", questions=(), summary=None):
    return {"name": "record_finding", "input": {
        "issue_key": key, "check_type": kind, "status": status, "is_blocking": blocking,
        "summary": summary or f"{key} is {status}", "elements": list(elements), "requirement_ids": list(reqs),
        "evidence_ids": list(evidence), "citations": [], "internal_checks": list(checks),
        "question_ids": list(questions), "impact": "", "recommended_action": action}}


SUBMIT = {"tool_calls": [{"name": "submit_review", "input": {"summary": "Reviewed."}}]}


def fail_round(key="grain:fact_sales", elements=("fact_sales",)):
    return [{"tool_calls": [finding(key, "failed", blocking=True, elements=elements,
                                    action="Declare the grain")]}, SUBMIT]


def pass_round(key="grain:fact_sales"):
    return [{"tool_calls": [finding(key, "passed", elements=["fact_sales"], checks=["fact_grain_declared"])]}, SUBMIT]


def results(transcript):
    out = []
    for t in transcript:
        if t["role"] == "tool":
            out += [{"is_error": r["is_error"], "content": r["content"]} for r in t["results"]]
    return out


def test_a_passing_review_saves_checks_and_marks_ready_for_review(proj, db_session_factory):
    validator_steps = [
        {"tool_calls": [{"name": "get_candidate", "input": {}}]},
        {"tool_calls": [{"name": "list_sources", "input": {}}]},
        lambda t: {"tool_calls": [{"name": "source_candidate_key_check", "input": {
            "source_id": last_result(t)["sources"][0]["source_id"], "table": "order_lines",
            "columns": ["order_line_id"], "purpose": "confirm the sales grain"}}]},
        lambda t: {"tool_calls": [
            finding("grain:fact_sales", elements=["fact_sales"], evidence=[last_result(t)["evidence_id"]]),
            finding("coverage:REQ-001", kind="coverage", elements=["fact_sales.net_amount"],
                    reqs=[proj.revenue_req], checks=["requirements_mapped"])]},
        SUBMIT,
    ]
    _, _, validator = proj.script_agents(_fix_seq(proj, design_steps(proj)), [], validator_steps)
    events = proj.run()
    done = events[-1]
    assert done["status"] == "completed", done
    assert done["candidate"]["ready_for_review"] is True

    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    assert snap["state"]["effective_state"] == "ready_for_review"
    checks = {c["data"]["issue_key"]: c for c in snap["checks"]}
    assert set(checks) == {"grain:fact_sales", "coverage:REQ-001"}
    grain = checks["grain:fact_sales"]["data"]
    assert grain["status"] == "passed" and grain["candidate_id"] == done["candidate"]["id"]
    assert grain["evidence"][0]["kind"] == "source" and grain["evidence"][0]["tool"] == "candidate_key_check"
    fact = next(t for t in snap["tables"] if t["data"]["name"] == "fact_sales")
    links = {(l["from_id"], l["to_id"], l["link_type"]) for l in snap["links"]}
    assert (checks["grain:fact_sales"]["id"], fact["id"], "checks") in links
    assert (checks["coverage:REQ-001"]["id"], proj.revenue_req, "checks") in links

    # The Validator worked read-only, as its own run under the Architect's, and its activity streamed.
    tools = set(validator.calls[0]["tools"])
    assert {"record_finding", "submit_review", "source_relationship_check"} <= tools
    assert not tools & {"put_table", "put_relationships", "remove_element", "map_requirement", "record_decision",
                        "update_feasibility", "raise_question", "ask_requirements", "record_definition"}
    assert any(e.get("type") == "tool" and e.get("agent") == "validator" for e in events)
    assert [e["status"] for e in events if e["type"] == "review"] == ["started", "completed"]
    with db_session_factory() as s:
        child = s.scalar(select(AgentRun).where(AgentRun.agent == "validator"))
        assert child.parent_run_id is not None and child.status == "completed" and child.iterations == 5
    reply = proj.alice.get(f"{proj.base}/conversations/{proj.conv['id']}/messages").json()[-1]["text"]
    assert "2 passed, 0 failed, 0 unverified" in reply and "Ready for Review" in reply


def test_a_failed_finding_is_corrected_and_revalidated(proj):
    no_grain = {**FACT_SALES, "grain": ""}
    architect_steps = [*_fix_seq(proj, design_steps(proj, fact=no_grain)),
                       {"tool_calls": [{"name": "put_table", "input": FACT_SALES}]},
                       {"text": "Declared the order-line grain on fact_sales."}]
    architect, _, validator = proj.script_agents(architect_steps, [], [*fail_round(), *pass_round()])
    done = proj.run()[-1]
    assert done["candidate"]["ready_for_review"] is True, done

    cand = proj.alice.get(f"{proj.base}/candidates/{done['candidate']['id']}").json()
    assert [r["round"] for r in cand["validation"]] == [0, 1]
    assert [r["findings"][0]["status"] for r in cand["validation"]] == ["failed", "passed"]
    assert cand["issue_attempts"] == {"grain:fact_sales": 1}
    # The Architect got the finding as a targeted message; the Validator re-checked it by key,
    # with the Architect's account of the fix as a claim to verify.
    correction = architect.calls[6]["transcript"][-1]["text"]
    assert "<validator_findings" in correction and "grain:fact_sales" in correction and "attempt 1 of 3" in correction
    brief = validator.calls[2]["transcript"][0]["text"]
    assert "round 1" in brief and "grain:fact_sales [failed, blocking]" in brief
    assert "Declared the order-line grain" in brief
    reply = proj.alice.get(f"{proj.base}/conversations/{proj.conv['id']}/messages").json()[-1]["text"]
    assert "after 1 correction round" in reply


def test_corrections_stop_at_the_limits_and_escalate(proj):
    # The Validator keeps failing the same issue; the Architect can't fix it.
    no_grain = {**FACT_SALES, "grain": ""}
    architect_steps = [*_fix_seq(proj, design_steps(proj, fact=no_grain)),
                       {"text": "Tried."}, {"text": "Tried again."}, {"text": "Still tried."}]
    proj.script_agents(architect_steps, [], fail_round() * 4)
    done = proj.run()[-1]
    assert done["candidate"]["ready_for_review"] is False
    cand = proj.alice.get(f"{proj.base}/candidates/{done['candidate']['id']}").json()
    assert len(cand["validation"]) == 4  # round 0 plus three correction rounds, then stop
    assert cand["issue_attempts"] == {"grain:fact_sales": 3}
    assert any("grain:fact_sales" in b for b in cand["blockers"])

    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    assert snap["state"]["effective_state"] == "draft"
    [check] = snap["checks"]
    assert check["data"]["status"] == "failed" and check["data"]["attempts"] == 3
    [question] = [q for q in snap["questions"] if q["data"]["kind"] == "validation_finding"]
    assert "still failing after 3 correction attempts" in question["data"]["description"]
    assert question["data"]["is_blocking"] is True and question["data"]["proposed_action"] == "Declare the grain"

    # A new episode on the same model: the issue's allowance is used up, so it isn't
    # corrected again, and the open question isn't duplicated.
    architect, _, _ = proj.script_agents([{"text": "Reviewed the model again."}], [], fail_round())
    done = proj.run("Check the model again")[-1]
    cand = proj.alice.get(f"{proj.base}/candidates/{done['candidate']['id']}").json()
    assert len(cand["validation"]) == 1 and cand["issue_attempts"] == {"grain:fact_sales": 3}
    assert len(architect.calls) == 1  # no correction round
    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    assert len([q for q in snap["questions"] if q["data"]["kind"] == "validation_finding"]) == 1


def test_findings_on_person_edited_elements_become_questions(proj):
    # A person modeled dim_store; the Architect adds fact_sales.
    proj.alice.post(f"{proj.base}/changesets", json={"model_id": proj.model, "operations": [
        {"op": "create", "type": "table", "ref": "s", "data": {"name": "dim_store", "kind": "dimension"}},
        {"op": "create", "type": "attribute", "data": {"table_id": "$s", "name": "store_key", "logical_type": "integer",
                                                        "role": "surrogate_key"}}]})
    no_grain = {**FACT_SALES, "grain": "", "attributes": FACT_SALES["attributes"][1:]}
    seen = {}

    def correction(t):
        return {"tool_calls": [{"name": "put_table", "input": {
            "name": "dim_store", "previous_name": "", "kind": "dimension", "grain": "", "description": "Stores",
            "scd_type": "type_1", "source_ref": "", "attributes": []}}]}

    def after_refusal(t):
        seen["refusal"] = results(t)[-1]
        return {"tool_calls": [{"name": "put_table", "input": FACT_SALES}]}

    architect_steps = [{"tool_calls": [{"name": "put_table", "input": no_grain}]}, {"text": "Added fact_sales."},
                       correction, after_refusal, {"text": "Declared the grain; left dim_store alone."}]
    person = finding("conventions:dim_store", "failed", elements=["dim_store"], kind="conventions",
                     action="Add a natural key")
    validator_steps = [
        {"tool_calls": [person, *fail_round()[0]["tool_calls"]]}, SUBMIT,
        {"tool_calls": [person, *pass_round()[0]["tool_calls"]]}, SUBMIT,
    ]
    proj.script_agents(architect_steps, [], validator_steps)
    done = proj.run()[-1]
    assert done["candidate"]["status"] == "proposed"

    assert seen["refusal"]["is_error"] and "a person edited dim_store" in seen["refusal"]["content"]
    cand = proj.alice.get(f"{proj.base}/candidates/{done['candidate']['id']}").json()
    assert cand["issue_attempts"] == {"grain:fact_sales": 1}  # the person's element was never "corrected"
    last = {f["issue_key"]: f for f in cand["validation"][-1]["findings"]}
    assert last["conventions:dim_store"]["edited_by_person"] is True
    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    [question] = [q for q in snap["questions"] if q["data"]["kind"] == "validation_finding"]
    assert "you edited dim_store" in question["data"]["description"]
    store = next(t for t in snap["tables"] if t["data"]["name"] == "dim_store")
    assert (question["id"], store["id"], "concerns") in {(l["from_id"], l["to_id"], l["link_type"]) for l in snap["links"]}


def test_a_pass_needs_evidence_and_blocking_unverified_keeps_draft(proj):
    seen = {}

    def capture(t):
        seen["results"] = results(t)
        return SUBMIT

    validator_steps = [
        {"tool_calls": [finding("grain:fact_sales", elements=["fact_sales"])]},  # passed, no evidence
        {"tool_calls": [finding("grain:fact_sales", elements=["no_such_table"], checks=["fact_grain_declared"])]},
        {"tool_calls": [finding("grain:fact_sales", elements=["fact_sales"], evidence=["not-an-id"])]},
        {"tool_calls": [finding("joins:fact_sales", "unverified", blocking=True, elements=["fact_sales.store_key"],
                                kind="keys_and_joins")]},
        capture,
    ]
    proj.script_agents(_fix_seq(proj, design_steps(proj)), [], validator_steps)
    done = proj.run()[-1]
    no_evidence, unknown, bad_id, ok = seen["results"]
    assert no_evidence["is_error"] and "unverified" in no_evidence["content"]
    assert unknown["is_error"] and "not in the candidate" in unknown["content"]
    assert bad_id["is_error"] and not ok["is_error"]
    assert done["candidate"]["ready_for_review"] is False
    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    assert snap["state"]["effective_state"] == "draft"
    assert [c["data"]["status"] for c in snap["checks"]] == ["unverified"]
    assert any("couldn't be verified" in q["data"]["description"] for q in snap["questions"])


def test_without_a_validator_the_design_is_unverified_and_stays_draft(proj):
    proj.script(*_fix_seq(proj, design_steps(proj)))
    done = proj.run()[-1]
    assert done["candidate"]["status"] == "applied" and done["candidate"]["ready_for_review"] is False
    reply = proj.alice.get(f"{proj.base}/conversations/{proj.conv['id']}/messages").json()[-1]["text"]
    assert "Not validated: no model is configured for the validator agent" in reply
    assert proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()["checks"] == []


def test_a_validator_provider_failure_leaves_the_design_unverified(proj, db_session_factory):
    class Broken(ScriptedChatModel):
        def complete(self, *a, **k):
            raise LLMError("OpenAI API error 500: boom")

    a = ScriptedChatModel(_fix_seq(proj, design_steps(proj)))
    proj.client.app.state.chat_model_factory = lambda agent: {"architect": a, "business_requirements": a,
                                                              "validator": Broken([])}[agent]
    done = proj.run()[-1]
    assert done["status"] == "completed" and done["candidate"]["ready_for_review"] is False
    reply = proj.alice.get(f"{proj.base}/conversations/{proj.conv['id']}/messages").json()[-1]["text"]
    assert "review didn't complete (OpenAI API error 500: boom)" in reply
    with db_session_factory() as s:
        assert s.scalar(select(AgentRun).where(AgentRun.agent == "validator")).status == "failed"


def test_proposed_candidates_carry_their_review_to_apply(proj):
    proj.alice.post(f"{proj.base}/changesets", json={"model_id": proj.model, "operations": [
        {"op": "create", "type": "table", "ref": "s", "data": {"name": "dim_other", "kind": "dimension"}},
        {"op": "create", "type": "attribute", "data": {"table_id": "$s", "name": "other_key", "logical_type": "integer",
                                                        "role": "surrogate_key"}}]})
    proj.script_agents(_fix_seq(proj, design_steps(proj)), [], pass_round())
    done = proj.run()[-1]
    cand = proj.alice.get(f"{proj.base}/candidates/{done['candidate']['id']}").json()
    assert cand["status"] == "proposed" and cand["ready_for_review"] is True
    assert "1 validation checks (0 not passed)" in cand["diff"]["added"]
    proj.alice.post(f"{proj.base}/candidates/{cand['id']}/apply")
    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    assert snap["state"]["effective_state"] == "ready_for_review"
    assert [c["data"]["issue_key"] for c in snap["checks"]] == ["grain:fact_sales"]


def _proposed_reviewed_candidate(proj):
    proj.alice.post(f"{proj.base}/changesets", json={"model_id": proj.model, "operations": [
        {"op": "create", "type": "table", "ref": "s", "data": {"name": "dim_other", "kind": "dimension"}},
        {"op": "create", "type": "attribute", "data": {"table_id": "$s", "name": "other_key", "logical_type": "integer",
                                                        "role": "surrogate_key"}}]})
    proj.script_agents(_fix_seq(proj, design_steps(proj)), [], pass_round())
    cid = proj.run()[-1]["candidate"]["id"]
    cand = proj.alice.get(f"{proj.base}/candidates/{cid}").json()
    assert cand["ready_for_review"] is True and cand["review_outdated"] is False
    return cid


def _edit_requirement(proj, record_id, statement):
    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    seq = next(r["seq"] for r in snap["requirements"] if r["id"] == record_id)
    resp = proj.alice.post(f"{proj.base}/changesets", json={"operations": [
        {"op": "update", "id": record_id, "expected_seq": seq, "data": {"statement": statement}}]})
    assert resp.status_code == 201, resp.text


def test_a_requirement_changed_after_review_makes_the_review_out_of_date(proj):
    """The person can still apply the design, but the old review can't make it Ready for Review."""
    cid = _proposed_reviewed_candidate(proj)
    # The design maps the revenue requirement, which is not yet linked to anything in the model.
    _edit_requirement(proj, proj.revenue_req, "Monthly gross and net revenue by store")
    cand = proj.alice.get(f"{proj.base}/candidates/{cid}").json()
    assert cand["review_outdated"] is True and cand["stale"] is False and cand["ready_for_review"] is False
    assert any(b.startswith("review out of date") for b in cand["blockers"])

    applied = proj.alice.post(f"{proj.base}/candidates/{cid}/apply")
    assert applied.status_code == 200 and applied.json()["status"] == "applied"
    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    assert snap["state"]["effective_state"] == "draft"
    assert "fact_sales" in {t["data"]["name"] for t in snap["tables"]}


def test_unrelated_register_changes_leave_the_review_current(proj):
    cid = _proposed_reviewed_candidate(proj)
    # Not mapped by the design and not linked to the model; another model's requirement.
    other = proj.alice.post(f"{proj.base}/changesets", json={"operations": [
        {"op": "create", "type": "requirement", "ref": "c", "data": {"statement": "Churn by cohort",
                                                          "origin_kind": "user_statement"}}]}).json()["created"]
    _edit_requirement(proj, other["c"], "Churn by signup cohort")
    cand = proj.alice.get(f"{proj.base}/candidates/{cid}").json()
    assert cand["review_outdated"] is False and cand["ready_for_review"] is True
    proj.alice.post(f"{proj.base}/candidates/{cid}/apply")
    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    assert snap["state"]["effective_state"] == "ready_for_review"


def test_openai_adapter_translates_the_transcript(monkeypatch):
    """The Responses API mapping: replayed output items, function call outputs, stop reasons."""
    from types import SimpleNamespace as NS

    from app.adapters.llm import OpenAIChatModel, ToolSpec

    sent = {}

    class Client:
        class responses:  # noqa: N801
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)
                call = NS(type="function_call", call_id="c1", name="record_finding", arguments=json.dumps({"x": 1}),
                          model_dump=lambda **_: {"type": "function_call", "call_id": "c1", "name": "record_finding",
                                                  "arguments": "{\"x\": 1}"})
                msg = NS(type="message", content=[NS(type="output_text", text="Checking.")],
                         model_dump=lambda **_: {"type": "message", "role": "assistant", "content": []})
                return NS(output=[msg, call], status="completed", incomplete_details=None, model="gpt-6-astra",
                          usage=NS(input_tokens=10, output_tokens=5, input_tokens_details=NS(cached_tokens=4)))

    m = OpenAIChatModel.__new__(OpenAIChatModel)
    import openai
    m._client, m._openai, m.model, m.effort, m.max_tokens = Client(), openai, "gpt-6-astra", "high", 100
    transcript = [
        {"role": "user", "text": "Review it."},
        {"role": "assistant", "text": "", "tool_calls": [], "raw": [{"type": "reasoning", "encrypted_content": "e"}]},
        {"role": "tool", "results": [{"id": "c0", "content": "boom", "is_error": True}]},
    ]
    turn = m.complete("sys", transcript, [ToolSpec("record_finding", "d", {"type": "object"}, strict=True)])
    assert sent["instructions"] == "sys" and sent["store"] is False
    assert sent["input"][1] == {"type": "reasoning", "encrypted_content": "e"}
    assert sent["input"][2] == {"type": "function_call_output", "call_id": "c0", "output": "Error: boom"}
    assert sent["tools"][0]["strict"] is True and sent["reasoning"] == {"effort": "high"}
    assert turn.stop_reason == "tool_use" and turn.tool_calls[0].input == {"x": 1} and turn.text == "Checking."
    assert turn.usage.cache_read_tokens == 4 and len(turn.raw) == 2


def test_the_round_limit_applies_per_episode_before_the_issue_limit(proj, monkeypatch):
    monkeypatch.setattr(get_settings(), "validator_max_rounds", 1)
    no_grain = {**FACT_SALES, "grain": ""}
    architect_steps = [*_fix_seq(proj, design_steps(proj, fact=no_grain)), {"text": "Tried."}, {"text": "Again."}]
    proj.script_agents(architect_steps, [], fail_round() * 3)
    done = proj.run()[-1]
    cand = proj.alice.get(f"{proj.base}/candidates/{done['candidate']['id']}").json()
    assert len(cand["validation"]) == 2 and cand["issue_attempts"] == {"grain:fact_sales": 1}


def _open_question(p, text="Which net revenue definition applies?"):
    made = p.alice.post(f"{p.base}/changesets", json={"operations": [
        {"op": "create", "type": "question", "ref": "q", "data": {"kind": "contradiction", "description": text}}]})
    return made.json()["created"]["q"]


def _validation_questions(p):
    snap = p.alice.get(f"{p.base}/models/{p.model}/snapshot").json()
    return snap, [q for q in snap["questions"] if q["data"]["kind"] == "validation_finding"]


def test_findings_that_rest_on_an_open_question_link_to_it(proj):
    q1 = _open_question(proj)
    seen = {}

    def capture(t):
        seen["results"] = results(t)
        return SUBMIT

    validator_steps = [
        {"tool_calls": [finding("definition:net_revenue", "unverified", blocking=True, kind="definition_consistency",
                                elements=["fact_sales.net_amount"], questions=[proj.revenue_req])]},  # not a question
        {"tool_calls": [finding("definition:net_revenue", "unverified", blocking=True, kind="definition_consistency",
                                elements=["fact_sales.net_amount"], questions=[q1])]},
        capture,
    ]
    proj.script_agents(_fix_seq(proj, design_steps(proj)), [], validator_steps)
    done = proj.run()[-1]
    wrong, ok = seen["results"]
    assert wrong["is_error"] and "not an open question" in wrong["content"] and not ok["is_error"]
    assert done["candidate"]["ready_for_review"] is False  # still blocking: the question is open

    snap, raised = _validation_questions(proj)
    assert raised == []  # no duplicate of the open question
    [check] = snap["checks"]
    links = {(l["from_id"], l["to_id"], l["link_type"]) for l in snap["links"]}
    assert (check["id"], q1, "checks") in links
    code = next(q["code"] for q in snap["questions"] if q["id"] == q1)
    reply = proj.alice.get(f"{proj.base}/conversations/{proj.conv['id']}/messages").json()[-1]["text"]
    assert f"linked to {code}, already open" in reply


def test_a_reworded_finding_on_an_escalated_issue_links_to_its_question(proj):
    blocking = dict(kind="keys_and_joins", blocking=True, elements=["fact_sales.store_key"])
    proj.script_agents(_fix_seq(proj, design_steps(proj)), [], [
        {"tool_calls": [finding("joins:store", "unverified", summary="Store join not checked.", **blocking)]}, SUBMIT])
    proj.run()
    snap, [question] = _validation_questions(proj)
    assert question["data"]["description"].startswith("Validation finding joins:store:")
    assert (snap["checks"][0]["id"], question["id"], "checks") in {
        (l["from_id"], l["to_id"], l["link_type"]) for l in snap["links"]}

    # Next episode: the same issue, worded differently.
    proj.script_agents([{"text": "Reviewed."}], [], [
        {"tool_calls": [finding("joins:store", "unverified", summary="Couldn't confirm stores join cleanly.",
                                **blocking)]}, SUBMIT])
    done = proj.run("Check it again")[-1]
    _, again = _validation_questions(proj)
    assert [q["id"] for q in again] == [question["id"]]
    cand = proj.alice.get(f"{proj.base}/candidates/{done['candidate']['id']}").json()
    assert cand["validation"][-1]["findings"][0]["question_ids"] == [question["id"]]
