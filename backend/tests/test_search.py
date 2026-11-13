"""Selective Tree of Thought at Architect decision points: a bounded, local beam search with
separate generation, evaluation and control; isolated node workspaces; pruning records;
budgets; honest selection; and the decision recorded in the register. Scripted models and a
fake source. One scripted model plays the Architect, the generator and the evaluator, in call
order."""

import copy

from sqlalchemy import select

from app.config import get_settings
from app.persistence.conversations import AgentRun
from app.persistence.search import DecisionPoint, SearchNode
from app.persistence.sources import SourceEvidence
from tests.test_architect import DIM_STORE, FACT_SALES, REL, proj  # noqa: F401


def attr(name, role, logical_type="integer", additivity="none"):
    return {"name": name, "logical_type": logical_type, "role": role, "is_nullable": False,
            "additivity": additivity, "description": "", "source_ref": ""}


def fact(name, grain, measure):
    return {"name": name, "previous_name": "", "kind": "fact", "grain": grain, "description": "", "scd_type": "none",
            "source_ref": "store_daily_costs", "attributes": [
                attr("store_key", "foreign_key"), attr(measure, "measure", "decimal", "additive")]}


STORE_DAY = fact("fact_store_profit", "One row per store per day", "operating_cost")
ORDER_ALLOC = fact("fact_order_profit", "One row per order", "allocated_cost")


def alt(choice, tables=(), remove=(), rels=(), mappings=(), assumptions=()):
    return {"choice": choice, "rationale": f"because {choice}", "evidence_ids": [], "assumptions": list(assumptions),
            "tables": list(tables), "remove_tables": list(remove), "relationships": list(rels),
            "mappings": list(mappings)}


def propose(*alts):
    return {"tool_calls": [{"name": "propose_alternatives", "input": {"alternatives": list(alts)}}]}


def score(*entries):
    return {"tool_calls": [{"name": "score_candidates", "input": {"scores": [
        {"candidate": c, "coverage": s, "evidence": s, "conventions": s, "simplicity": s, "hard_violation": hv,
         "unverified_critical": u, "assessment": f"{c} scored {s}"} for c, s, hv, u in entries]}}]}


def decision_point(p, stages=("grain",), elements=("fact_store_profit", "fact_order_profit"), trigger="grain_conflict",
                   alternatives=2, issue_key=""):
    return {"name": "open_decision_point", "input": {
        "trigger": trigger, "question": "How should profit be modelled when costs exist only by store and day?",
        "why": "Costs are in store_daily_costs (store x day x cost type); orders have no cost.",
        "elements": list(elements), "requirement_ids": [p.profit_req], "stages": list(stages),
        "alternatives": [{"choice": f"option {i}", "rationale": "viable"} for i in range(alternatives)],
        "issue_key": issue_key}}


BASE = [{"tool_calls": [{"name": "put_table", "input": DIM_STORE}, {"name": "put_table", "input": FACT_SALES}]},
        {"tool_calls": [{"name": "put_relationships", "input": {"relationships": [REL]}}]}]


def _results(transcript):
    return [r for t in transcript if t["role"] == "tool" for r in t["results"]]


def test_a_grain_conflict_is_searched_locally_and_the_choice_recorded(proj, monkeypatch, db_session_factory):
    monkeypatch.setattr(get_settings(), "tot_children", 4)
    out_of_scope = {**DIM_STORE, "description": "Changed by a branch"}
    seen = {}

    def finish(t):
        seen["result"] = _results(t)[-1]
        return {"text": "Modelled store-day operating profit; profit by order stays unsupported."}

    m = proj.script(
        *BASE,
        {"tool_calls": [decision_point(proj)]},
        # generation: one call for the root, four alternatives
        propose(alt("Store-day operating profit fact", tables=[STORE_DAY]),
                alt("Allocate store costs to orders", tables=[ORDER_ALLOC], assumptions=["Costs split by revenue"]),
                alt("Rework the store dimension", tables=[out_of_scope]),
                alt("Store-day profit (same design again)", tables=[STORE_DAY])),
        # evaluation: a source probe, then scores
        {"tool_calls": [{"name": "source_candidate_key_check", "input": {
            "source_id": proj.source, "table": "store_daily_costs", "columns": ["store_id", "cost_date", "cost_type"],
            "purpose": "confirm the cost grain"}}]},
        score(("C1", 4, "", 0), ("C2", 4, "Allocates costs to orders without an agreed method", 1)),
        finish,
    )
    done = proj.run()[-1]
    assert done["status"] == "completed", done
    assert seen["result"]["is_error"] is False

    with db_session_factory() as s:
        dp = s.scalar(select(DecisionPoint))
        assert dp.status == "selected" and dp.trigger == "grain_conflict" and dp.stop_reason == "depth limit"
        assert dp.usage["model_calls"] == 3 and dp.usage["tool_calls"] == 1
        nodes = {n.choice: n for n in s.scalars(select(SearchNode).where(SearchNode.depth == 1))}
        assert nodes["Store-day operating profit fact"].status == "selected"
        assert nodes["Allocate store costs to orders"].status == "rejected"
        assert "without an agreed method" in nodes["Allocate store costs to orders"].reason
        assert nodes["Rework the store dimension"].status == "rejected"
        assert "doesn't affect" in nodes["Rework the store dimension"].reason
        assert nodes["Store-day profit (same design again)"].status == "duplicate"
        # Each node is an isolated workspace: the rejected branch's table never reached the model.
        assert any(t["name"] == "fact_order_profit" for t in nodes["Allocate store costs to orders"].content["tables"])
        ev = s.scalar(select(SourceEvidence).where(SourceEvidence.tool == "candidate_key_check"))
        assert ev.purpose == "confirm the cost grain"
        run = s.scalar(select(AgentRun).where(AgentRun.agent == "architect"))
        assert run.input_tokens == 100 * len(m.calls)  # the search's calls count toward the run

    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    names = {t["data"]["name"] for t in snap["tables"]}
    assert "fact_store_profit" in names and "fact_order_profit" not in names
    assert next(t for t in snap["tables"] if t["data"]["name"] == "dim_store")["data"]["description"] == "Stores"
    [decision] = snap["decisions"]
    assert decision["data"]["statement"].endswith("Chosen: Store-day operating profit fact.")
    assert "Allocate store costs to orders" in decision["data"]["alternatives"]
    assert "grain conflict" in decision["data"]["rationale"] and "Not chosen" in decision["data"]["rationale"]
    links = {(l["from_id"], l["to_id"], l["link_type"]) for l in snap["links"]}
    assert (decision["id"], proj.profit_req, "affects") in links
    profit = next(t for t in snap["tables"] if t["data"]["name"] == "fact_store_profit")
    assert (decision["id"], profit["id"], "affects") in links

    # The generator saw only the affected part of the design in full.
    brief = m.calls[3]["transcript"][0]["text"]
    assert "<decision_brief>" in brief and '"other_tables"' in brief and '"fact fact_sales"' in brief
    assert m.calls[3]["tools"] == ["propose_alternatives"]
    assert "score_candidates" in m.calls[4]["tools"] and "put_table" not in m.calls[4]["tools"]


def test_beam_pruning_threshold_and_tie_breaks_across_stages(proj, db_session_factory):
    a = fact("fact_store_profit", "One row per store per day", "operating_cost")
    b = fact("fact_store_profit", "One row per store per month", "operating_cost")
    c = fact("fact_store_profit", "One row per store per week", "operating_cost")
    measure_alt = lambda name, base: alt(name, tables=[{**base, "attributes": [*base["attributes"],  # noqa: E731
                                                                               attr("revenue", "measure", "decimal", "additive")]}])
    proj.script(
        *BASE,
        {"tool_calls": [decision_point(proj, stages=("grain", "measures"), elements=("fact_store_profit",))]},
        propose(alt("Daily grain", tables=[a]), alt("Monthly grain", tables=[b], assumptions=["Month is enough"]),
                alt("Weekly grain", tables=[c])),
        # Daily and monthly tie on score; daily has fewer assumptions. Weekly is below the threshold.
        score(("C1", 4, "", 0), ("C2", 4, "", 0), ("C3", 2, "", 0)),
        propose(measure_alt("Daily with revenue", a)),  # expands the daily branch
        propose(measure_alt("Monthly with revenue", b)),  # expands the monthly branch
        score(("C1", 3, "", 1), ("C2", 3, "", 0)),  # tie broken by fewer unverified critical checks
        {"text": "Done."},
    )
    proj.run()
    with db_session_factory() as s:
        nodes = {n.choice: n for n in s.scalars(select(SearchNode))}
        assert nodes["Daily grain"].status == "kept" and "Rank 1" in nodes["Daily grain"].reason
        assert nodes["Monthly grain"].status == "kept" and "Rank 2" in nodes["Monthly grain"].reason
        assert nodes["Weekly grain"].status == "pruned" and "below the threshold" in nodes["Weekly grain"].reason
        assert nodes["Monthly with revenue"].status == "selected"
        assert nodes["Daily with revenue"].status == "kept"
        assert nodes["Monthly with revenue"].stage == "measures" and nodes["Monthly with revenue"].depth == 2
        assert s.scalar(select(DecisionPoint)).status == "selected"
        assert len([n for n in nodes.values() if n.depth]) <= 15


def test_a_budget_stop_applies_the_best_partial_and_blocks_readiness(proj, monkeypatch, db_session_factory):
    monkeypatch.setattr(get_settings(), "tot_max_model_calls", 2)
    proj.script(
        *BASE,
        {"tool_calls": [decision_point(proj, stages=("grain", "measures"), elements=("fact_store_profit",))]},
        propose(alt("Daily grain", tables=[STORE_DAY])),
        score(("C1", 4, "", 0)),
        {"text": "Done."},
    )
    done = proj.run()[-1]
    with db_session_factory() as s:
        dp = s.scalar(select(DecisionPoint))
        assert dp.status == "partial" and dp.stop_reason == "model-call budget reached"
    cand = proj.alice.get(f"{proj.base}/candidates/{done['candidate']['id']}").json()
    assert any(t["name"] == "fact_store_profit" for t in cand["content"]["tables"])
    assert any(b.startswith("decision point:") and "partial" in b for b in cand["blockers"])
    assert "Best partial design" in proj.alice.get(
        f"{proj.base}/models/{proj.model}/snapshot").json()["decisions"][0]["data"]["statement"]


def test_when_every_alternative_is_rejected_the_candidate_is_unchanged(proj, db_session_factory):
    proj.script(
        *BASE,
        {"tool_calls": [decision_point(proj)]},
        propose(alt("Allocate by revenue", tables=[ORDER_ALLOC]), alt("Allocate by orders", tables=[
            {**ORDER_ALLOC, "grain": "One row per order line"}])),
        score(("C1", 3, "No agreed allocation", 0), ("C2", 3, "No agreed allocation", 0)),
        {"text": "Profit by order is unsupported."},
    )
    done = proj.run()[-1]
    with db_session_factory() as s:
        assert s.scalar(select(DecisionPoint)).status == "no_candidate"
    snap = proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot").json()
    assert {t["data"]["name"] for t in snap["tables"]} == {"dim_store", "fact_sales"}
    assert "No alternative qualified" in snap["decisions"][0]["data"]["statement"]
    cand = proj.alice.get(f"{proj.base}/candidates/{done['candidate']['id']}").json()
    assert any("no candidate" in b for b in cand["blockers"])


def test_decision_points_are_guarded(proj, monkeypatch, db_session_factory):
    monkeypatch.setattr(get_settings(), "architect_max_decision_points", 1)
    seen = {}

    def finish(t):
        seen["results"] = _results(t)
        return {"text": "Done."}

    meaning = decision_point(proj)
    meaning["input"]["trigger"] = "business_meaning"
    proj.script(
        *BASE,
        {"tool_calls": [meaning]},
        {"tool_calls": [decision_point(proj, alternatives=1)]},
        {"tool_calls": [decision_point(proj, trigger="multiple_remediations", issue_key="grain:none")]},
        {"tool_calls": [decision_point(proj, elements=())]},
        {"tool_calls": [decision_point(proj)]},
        propose(alt("Store-day", tables=[STORE_DAY])), score(("C1", 4, "", 0)),
        {"tool_calls": [decision_point(proj)]},  # over the per-run limit
        finish,
    )
    proj.run()
    r = [x for x in seen["results"] if x["is_error"]]
    assert "ask_requirements" in r[0]["content"]
    assert "at least two viable alternatives" in r[1]["content"]
    assert "failed Validator finding" in r[2]["content"]
    assert "affected elements" in r[3]["content"]
    assert "all 1 decision points" in r[4]["content"]
    with db_session_factory() as s:
        assert len(s.scalars(select(DecisionPoint)).all()) == 1


def test_the_baseline_has_no_search(proj, monkeypatch):
    monkeypatch.setattr(get_settings(), "architect_tot_enabled", False)
    m = proj.script(*BASE, {"text": "Done."})
    proj.run()
    assert "open_decision_point" not in m.calls[0]["tools"]
    monkeypatch.setattr(get_settings(), "architect_tot_enabled", True)
    m = proj.script({"text": "Done."})
    proj.run("Again")
    assert "open_decision_point" in m.calls[0]["tools"]


def test_apply_alternative_keeps_the_search_local():
    from app.agents.architect.search import Workspace, apply_alternative

    content = {"tables": [{"name": "dim_store", "kind": "dimension", "grain": None, "description": None,
                           "scd_type": "type_1", "source_ref": None, "attributes": []}],
               "relationships": [], "mappings": [], "decision_links": [], "removed": []}
    parent = Workspace(copy.deepcopy(content))
    scope = {"elements": ["fact_x"], "requirement_ids": []}
    _, why = apply_alternative(parent, alt("remove", remove=["dim_store"]), scope, [])
    assert "doesn't affect" in why
    ws, why = apply_alternative(parent, alt("add", tables=[STORE_DAY]), scope, [])
    assert why is None and parent.content == content  # the parent is untouched
    assert {t["name"] for t in ws.content["tables"]} == {"dim_store", "fact_store_profit"}


def test_only_the_beam_survives_a_level(proj, db_session_factory):
    days = [fact("fact_store_profit", f"One row per store per {g}", "operating_cost") for g in ("day", "week", "month")]
    proj.script(
        *BASE,
        {"tool_calls": [decision_point(proj, elements=("fact_store_profit",))]},
        propose(*(alt(f"Grain {i}", tables=[t]) for i, t in enumerate(days))),
        score(("C1", 5, "", 0), ("C2", 4, "", 0), ("C3", 3, "", 0)),  # all eligible
        {"text": "Done."},
    )
    proj.run()
    with db_session_factory() as s:
        nodes = {n.choice: n for n in s.scalars(select(SearchNode).where(SearchNode.depth == 1))}
        assert nodes["Grain 0"].status == "selected" and nodes["Grain 1"].status == "kept"
        assert nodes["Grain 2"].status == "pruned" and "Outside the beam" in nodes["Grain 2"].reason


def test_a_complete_candidate_that_still_fails_a_hard_check_is_only_partial(proj, db_session_factory):
    no_grain = {**FACT_SALES, "grain": ""}  # an existing hard failure the decision doesn't touch
    proj.script(
        {"tool_calls": [{"name": "put_table", "input": DIM_STORE}, {"name": "put_table", "input": no_grain}]},
        {"tool_calls": [decision_point(proj, elements=("fact_store_profit",))]},
        propose(alt("Store-day", tables=[STORE_DAY])),
        score(("C1", 4, "", 0)),
        {"text": "Done."},
    )
    proj.run()
    with db_session_factory() as s:
        # Not blamed on the branch (the root already failed it), but not a qualifying proposal either.
        assert s.scalar(select(SearchNode).where(SearchNode.choice == "Store-day")).status == "selected"
        assert s.scalar(select(DecisionPoint)).status == "partial"


def test_decision_points_are_inspectable_through_the_api(proj, as_user):
    proj.script(
        *BASE,
        {"tool_calls": [decision_point(proj)]},
        propose(alt("Store-day", tables=[STORE_DAY]), alt("Allocate", tables=[ORDER_ALLOC])),
        score(("C1", 4, "", 0), ("C2", 2, "", 0)),
        {"text": "Done."},
    )
    cid = proj.run()[-1]["candidate"]["id"]
    [dp] = proj.alice.get(f"{proj.base}/candidates/{cid}/decision-points").json()
    assert dp["status"] == "selected" and dp["trigger"] == "grain_conflict"
    by_choice = {n["choice"]: n for n in dp["nodes"]}
    assert by_choice["Current design"]["status"] == "root" and by_choice["Current design"]["tables"] == []
    assert by_choice["Store-day"]["tables"] == ["fact_store_profit"] and by_choice["Store-day"]["status"] == "selected"
    assert by_choice["Allocate"]["status"] == "pruned" and by_choice["Allocate"]["evaluation"]["total"] == 2.0
    bob = as_user("auth0|bob")  # another tenant's user can't see it
    assert bob.get(f"{proj.base}/candidates/{cid}/decision-points").status_code == 404
