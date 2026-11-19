"""The routing eval (eval/routing): the labelled set is well formed, briefs use the router's own
formatters, and scoring, consistency and the report work, with a scripted router model."""

import sys
from pathlib import Path

import pytest

from app.adapters.llm import LLMError, ScriptedChatModel

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval" / "routing"))
import routing_eval  # noqa: E402


def call(route):
    return {"tool_calls": [{"name": "route_request", "input": {"route": route, "reason": "Because.", "question": None}}]}


def test_the_labelled_set_is_well_formed_and_covers_every_route():
    data = routing_eval.load()
    intended = {c["expect"][0] for c in data["cases"]}
    assert intended == {"requirements", "architect", "requirements_then_architect", "clarify"}
    assert len(data["cases"]) >= 40


def test_a_malformed_case_is_refused():
    data = routing_eval.load()
    data["cases"].append({"id": "x", "state": "drafted", "expect": ["manager"], "message": "hi"})
    with pytest.raises(ValueError, match="expect must list routes"):
        routing_eval.check(data)


def test_briefs_match_what_the_router_reads():
    data = routing_eval.load()
    case = next(c for c in data["cases"] if c["id"] == "follow-01")
    text = routing_eval.case_brief(data, case)
    assert "<project_state>" in text and "Model tables: 7" in text
    assert "- User: Change fact_sales to order-line grain. → answered by Architect" in text
    assert text.endswith("<message>\nYes, go ahead.\n</message>")


def test_scoring_consistency_and_report():
    data = {"states": routing_eval.load()["states"], "cases": [
        {"id": "a", "state": "drafted", "expect": ["requirements"], "message": "Define net sales."},
        {"id": "b", "state": "drafted", "expect": ["clarify", "architect"], "message": "Fix it"},
    ]}

    def provider_error(_):
        raise LLMError("overloaded")

    model = ScriptedChatModel([call("requirements"), call("architect"),       # repeat 1: both right
                               call("architect"), {"text": "no tool call"}],  # repeat 2: a miss, a fallback
                              model="scripted-router")
    results = routing_eval.run_cases(model, data, repeats=2)
    assert [(r["id"], r["route"], r["correct"], r["strict"]) for r in results] == [
        ("a", "requirements", True, True), ("b", "architect", True, False),
        ("a", "architect", False, False), ("b", "clarify", True, True)]
    assert results[3]["fallback"] is True
    s = routing_eval.summarize(results)
    assert s["accuracy"] == 75.0 and s["strict_accuracy"] == 50.0 and s["consistent_cases"] == 0.0
    assert s["confusion"]["requirements"] == {"requirements": 1, "architect": 1} and s["fallbacks"] == 1
    md = routing_eval.report(s, results)
    assert "## Misses (1)" in md and "| a | 2 | requirements | architect |" in md

    errored = routing_eval.run_cases(ScriptedChatModel([provider_error]), {**data, "cases": data["cases"][:1]})
    assert errored[0]["route"] is None and not errored[0]["correct"] and "overloaded" in errored[0]["error"]
