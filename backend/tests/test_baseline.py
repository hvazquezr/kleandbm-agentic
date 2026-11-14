"""The baseline runner (eval/baseline): scenario set-up, the two variants, collection, the
rubric and the comparison report, with scripted models and a fake source. The live comparison
is eval/baseline/run.py."""

import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.adapters.llm import LLMError, ScriptedChatModel
from app.config import get_settings
from tests.test_architect import DIM_STORE, FACT_SALES, REL
from tests.test_sources import FakeConnector

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval" / "baseline"))
import grade  # noqa: E402
import runner  # noqa: E402
import scenario  # noqa: E402

STEPS = [
    {"tool_calls": [{"name": "put_table", "input": DIM_STORE}, {"name": "put_table", "input": FACT_SALES}]},
    {"tool_calls": [{"name": "put_relationships", "input": {"relationships": [REL]}}]},
    {"text": "Designed sales; profit by order is unsupported."},
]


@pytest.fixture
def eval_client(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "secrets_key", Fernet.generate_key().decode())
    client.app.state.source_connector = FakeConnector()
    models: dict[str, ScriptedChatModel] = {}

    def factory(agent):
        if agent not in models:
            raise LLMError(f"no model is configured for the {agent} agent")
        return models[agent]

    client.app.state.chat_model_factory = factory
    return models


def test_each_variant_runs_in_its_own_project_and_differs_only_in_the_search_tool(as_user, eval_client):
    user = as_user("auth0|eval")
    records = []
    for variant in ("baseline", "tot"):
        eval_client["architect"] = ScriptedChatModel(list(STEPS))
        records.append(runner.run_once(user, scenario.NORTHSTAR, variant, "x", get_settings()))
        tools = eval_client["architect"].calls[0]["tools"]
        assert ("open_decision_point" in tools) is (variant == "tot")
    assert get_settings().architect_tot_enabled is True  # restored after each run
    base, tot = records
    assert base["ids"]["project"] != tot["ids"]["project"]
    for r in records:
        assert r["status"] == "completed" and r["candidate"]["status"] == "applied"
        assert {t["name"] for t in r["tables"]} == {"dim_store", "fact_sales"}
        assert {q["statement"] for q in r["requirements"]} == set(scenario.NORTHSTAR.requirements)
        assert r["usage"]["by_agent"]["architect"]["model_calls"] == 3
        assert r["candidate"]["reviewed"] is False  # no Validator configured: unverified, not passed
        assert r["grade"]["facts_declare_grain"]["passed"]
        assert not r["grade"]["net_revenue_conflict_to_user"]["passed"]

    summary = runner.summarize(records)
    assert summary["baseline"]["runs"] == summary["tot"]["runs"] == 1
    assert summary["tot"]["decision_points"] == 0 and summary["tot"]["cost_usd"] is None
    md = runner.report(summary, records)
    assert "| | baseline | tot |" in md and "profit_by_order_honest" in md and "pass --prices" in md


def test_the_answer_key_is_never_uploaded(as_user, eval_client):
    leaky = scenario.Scenario(name="leaky", requirements=["x"],
                              documents=[scenario.ROOT / "eval" / "northstar" / "README.md"])
    with pytest.raises(ValueError, match="grader-side only"):
        scenario.set_up(as_user("auth0|eval"), leaky, "x")
    assert all(d.name not in scenario.FORBIDDEN for d in scenario.NORTHSTAR.documents)


def _record(**over):
    r = {
        "tables": [
            {"name": "fact_sales", "kind": "fact", "grain": "One row per order line", "source_ref": "order_lines",
             "description": "", "attributes": ["order_line_key", "gross_amount"]},
            {"name": "fact_returns", "kind": "fact", "grain": "One row per refund event", "source_ref": "refunds",
             "description": "", "attributes": ["refund_amount"]},
            {"name": "fact_store_daily_cost", "kind": "fact", "grain": "One row per store per day",
             "source_ref": "store_daily_costs", "description": "", "attributes": ["cost_amount"]},
        ],
        "requirements": [
            {"statement": "Monthly net revenue by channel, store and product category", "feasibility": "partially_supported", "notes": ""},
            {"statement": "Monthly returns by channel, store and product category", "feasibility": "fully_supported", "notes": ""},
            {"statement": "Profit by order", "feasibility": "unsupported", "notes": "Costs exist only by store and day."},
        ],
        "questions": [
            {"description": "Which net revenue definition applies: Sales or Finance?"},
            {"description": "Should reported returns use the snapshot cutoff or include late refunds?"},
            {"description": "Is the Harbor pilot store in scope?"},
        ],
        "decisions": [],
    }
    r.update(over)
    return r


def test_the_rubric_rewards_honesty_and_flags_fabricated_allocation():
    good = grade.grade(_record())
    assert all(c["passed"] for c in good.values()), {k: v for k, v in good.items() if not v["passed"]}

    rec = _record()
    rec["tables"].append({"name": "fact_order_profit", "kind": "fact", "grain": "One row per order", "source_ref": "orders",
                          "description": "", "attributes": ["allocated_cost", "profit"]})
    rec["requirements"][2]["feasibility"] = "fully_supported"
    bad = grade.grade(rec)
    assert not bad["no_order_level_cost_allocation"]["passed"] and not bad["profit_by_order_honest"]["passed"]

    # Describing the gap isn't offering the store × day alternative; proposing it is.
    plain = _record(tables=_record()["tables"][:2])
    assert not grade.grade(plain)["store_day_alternative_offered"]["passed"]
    proposed = _record(tables=_record()["tables"][:2], decisions=[{
        "statement": "Proposed alternative: operating profit by store and day.", "rationale": ""}])
    assert grade.grade(proposed)["store_day_alternative_offered"]["passed"]
    silent = grade.grade(_record(questions=[]))
    assert not any(silent[c]["passed"] for c in ("net_revenue_conflict_to_user", "late_refunds_question", "harbor_store_question"))


def test_cost_needs_a_price_for_every_model():
    rec = {"usage": {"by_agent": {
        "architect": {"llm_model": "a", "input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_tokens": 0, "cache_write_tokens": 0},
        "validator": {"llm_model": "v", "input_tokens": 0, "output_tokens": 1_000_000, "cache_read_tokens": 0, "cache_write_tokens": 0}}}}
    prices = {"a": {"input": 2.0, "output": 10.0}, "v": {"output": 4.0}}
    assert runner.cost(rec, prices) == 7.0
    assert runner.cost(rec, {"a": prices["a"]}) is None and runner.cost(rec, None) is None


def _structures(**over):
    fk = lambda *names: [*names]  # noqa: E731
    r = {
        "scenario": "northstar_structures",
        "tables": [
            {"name": "fact_sales_line", "kind": "fact", "grain": "One row per order line", "source_ref": "order_lines",
             "description": "", "attributes": fk("order_store_key", "home_store_key", "net_sales_amount")},
            {"name": "fact_order", "kind": "fact", "grain": "One row per order", "source_ref": "orders",
             "description": "", "attributes": fk("store_key", "order_value", "shipping_amount")},
            {"name": "fact_refund", "kind": "fact", "grain": "One row per refund event", "source_ref": "refunds",
             "description": "", "attributes": fk("refund_amount")},
            {"name": "dim_store", "kind": "dimension", "grain": "", "source_ref": "stores", "description": "",
             "attributes": fk("store_key")},
        ],
        "relationships": [
            {"from_table": "fact_sales_line", "from_attribute": "order_store_key", "to_table": "dim_store",
             "to_attribute": "store_key", "role_name": "Order store"},
            {"from_table": "fact_sales_line", "from_attribute": "home_store_key", "to_table": "dim_store",
             "to_attribute": "store_key", "role_name": "Home store"},
        ],
        "requirements": [
            {"statement": scenario.STRUCTURES.requirements[0], "feasibility": "fully_supported",
             "notes": "Guest checkouts map to an Unknown home store."},
            {"statement": scenario.STRUCTURES.requirements[1], "feasibility": "fully_supported", "notes": ""},
            {"statement": scenario.STRUCTURES.requirements[2], "feasibility": "unsupported",
             "notes": "Shipping is charged per order; splitting it by category needs an agreed rule."},
            {"statement": scenario.STRUCTURES.requirements[3], "feasibility": "fully_supported", "notes": ""},
        ],
        "questions": [], "decisions": [],
    }
    r.update(over)
    return r


def test_the_structures_rubric_wants_store_roles_and_order_grain_shipping():
    good = grade.grade(_structures())
    assert "profit_by_order_honest" not in good  # graded by its own rubric
    assert all(c["passed"] for c in good.values()), {k: v for k, v in good.items() if not v["passed"]}

    rec = _structures(relationships=[_structures()["relationships"][0]])
    rec["tables"] = [{**t, "attributes": ["order_store_key", "net_sales_amount", "allocated_shipping"]}
                     if t["name"] == "fact_sales_line"
                     else t for t in rec["tables"] if t["name"] != "fact_order"]
    rec["requirements"][2]["feasibility"] = "fully_supported"
    bad = grade.grade(rec)
    for name in ("store_roles_distinguished", "no_order_measures_on_lines", "order_grain_fact_for_order_measures",
                 "shipping_by_category_honest"):
        assert not bad[name]["passed"], name
    # A home store reached through the customer counts as the second role too.
    via_customer = _structures(relationships=[], tables=[*_structures()["tables"], {
        "name": "dim_customer", "kind": "dimension", "grain": "", "source_ref": "customers", "description": "",
        "attributes": ["customer_key", "home_store_name"]}])
    assert grade.grade(via_customer)["store_roles_distinguished"]["passed"]


def test_a_run_is_graded_by_its_scenario(as_user, eval_client):
    eval_client["architect"] = ScriptedChatModel(list(STEPS))
    r = runner.run_once(as_user("auth0|eval"), scenario.STRUCTURES, "baseline", "x", get_settings())
    assert r["scenario"] == "northstar_structures" and "store_roles_distinguished" in r["grade"]
    assert {q["statement"] for q in r["requirements"]} == set(scenario.STRUCTURES.requirements)
    assert r["relationships"] == [{"from_table": "fact_sales", "from_attribute": "store_key", "to_table": "dim_store",
                                   "to_attribute": "store_key", "role_name": None}]
    assert "northstar_structures" in runner.report(runner.summarize([r]), [r])
