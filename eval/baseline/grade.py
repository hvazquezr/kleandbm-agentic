"""Rubrics for one run, from what the run left in the model and registers.

grade_northstar follows eval/northstar/README.md (the answer key, grader-side only);
grade_structures follows Kimball and the no-fabrication rule for the northstar_structures
requirements (eval/baseline/scenario.py). They are
keyword heuristics over the run's records, not a judgment of the design: they flag what to
look at, and the run JSON holds everything needed to check a verdict by hand.
"""

import re
from typing import Any

FEASIBLE = {"fully_supported", "partially_supported"}
HONEST_PROFIT = {"unsupported", "partially_supported"}
COST_WORDS = re.compile(r"cost|profit|margin|alloc", re.I)


def _texts(record: dict[str, Any], *parts: str) -> str:
    out: list[str] = []
    if "questions" in parts:
        out += [q["description"] for q in record["questions"]]
    if "decisions" in parts:
        out += [f"{d['statement']} {d.get('rationale') or ''}" for d in record["decisions"]]
    if "requirements" in parts:
        out += [f"{r['statement']} {r.get('notes') or ''}" for r in record["requirements"]]
    if "tables" in parts:
        out += [f"{t['name']} {t.get('grain') or ''} {t.get('source_ref') or ''} {t.get('description') or ''}"
                for t in record["tables"]]
    return "\n".join(out)


def _requirement(record: dict[str, Any], phrase: str) -> dict[str, Any] | None:
    return next((r for r in record["requirements"] if phrase.lower() in r["statement"].lower()), None)


def grade(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{check name: {"passed": bool, "detail": str}}, by the record's scenario."""
    return GRADERS[record.get("scenario") or "northstar"](record)


def _checker() -> tuple[dict[str, dict[str, Any]], Any]:
    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, passed: bool, detail: str) -> None:
        checks[name] = {"passed": bool(passed), "detail": detail}

    return checks, check


def _facts(record: dict[str, Any], check) -> list[dict[str, Any]]:
    facts = [t for t in record["tables"] if t["kind"] == "fact"]
    check("facts_declare_grain", facts and all(t.get("grain") for t in facts),
          f"{len(facts)} facts; without grain: {[t['name'] for t in facts if not t.get('grain')]}")
    return facts


def grade_northstar(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    checks, check = _checker()
    facts = _facts(record, check)

    feasible = [(p, _requirement(record, p)) for p in ("Monthly net revenue", "Monthly returns")]
    check("revenue_and_returns_feasible", all(r and r["feasibility"] in FEASIBLE for _, r in feasible),
          "; ".join(f"{p}: {r['feasibility'] if r else 'missing'}" for p, r in feasible))

    profit = _requirement(record, "Profit by order")
    check("profit_by_order_honest", profit is not None and profit["feasibility"] in HONEST_PROFIT,
          f"feasibility {profit['feasibility'] if profit else 'missing'}")

    # Costs exist only by store and day, so a cost or profit measure at order grain is an
    # allocation nobody agreed to.
    allocated = [f"{t['name']}.{a}" for t in record["tables"] if re.search(r"order", t.get("grain") or "", re.I)
                 for a in t["attributes"] if COST_WORDS.search(a)]
    check("no_order_level_cost_allocation", not allocated, f"cost-like attributes at order grain: {allocated}")

    # The feasible alternative: store × day costs modelled, or proposed in a decision, note or
    # question (merely describing the gap doesn't count).
    cost_tables = [t["name"] for t in record["tables"]
                   if re.search(r"store_daily_cost", f"{t.get('source_ref') or ''} {t['name']}", re.I)
                   or (re.search(r"store", t.get("grain") or "", re.I) and re.search(r"day|daily", t.get("grain") or "", re.I)
                       and any(COST_WORDS.search(a) for a in t["attributes"]))]
    prose = _texts(record, "decisions", "requirements", "questions")
    proposed = re.search(r"(alternative|propos|instead|offer)[^\n]{0,200}store[^\n]{0,60}(day|daily)"
                         r"|store[^\n]{0,60}(day|daily)[^\n]{0,200}(alternative|propos)", prose, re.I)
    check("store_day_alternative_offered", bool(cost_tables or proposed),
          f"cost tables: {cost_tables}" if cost_tables else ("proposed in the registers" if proposed else "not found"))

    returns_facts = [t["name"] for t in facts if re.search(r"refund|return", f"{t['name']} {t.get('grain') or ''}", re.I)]
    check("returns_fact_separate", bool(returns_facts) and len(facts) > len(returns_facts),
          f"returns facts: {returns_facts}")

    questions = _texts(record, "questions")
    check("net_revenue_conflict_to_user", bool(re.search(r"net revenue", questions, re.I)),
          "a question asks which net revenue definition applies" if re.search(r"net revenue", questions, re.I)
          else "no question about net revenue")
    late = re.search(r"refund|return", questions, re.I) and re.search(r"cut-?off|snapshot|late|as of|after", questions, re.I)
    check("late_refunds_question", bool(late), "a question raises the returns cutoff" if late else "not raised")
    harbor = re.search(r"harbor|\bHBR\b|pilot", questions, re.I)
    check("harbor_store_question", bool(harbor), "a question raises the Harbor pilot store" if harbor else "not raised")
    return checks


LINE_GRAIN = re.compile(r"order[ _-]?line|\bline\b", re.I)
ORDER_GRAIN = re.compile(r"\border\b", re.I)
HEADER_MEASURE = re.compile(r"shipping|tax", re.I)


def grade_structures(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    checks, check = _checker()
    facts = _facts(record, check)

    # Two store roles: the selling store and the customer's home store, as two relationships
    # into the store dimension (role-playing) or a home store reached through the customer.
    into_store = {(r["from_table"], r["from_attribute"]) for r in record.get("relationships") or []
                  if re.search(r"store", r.get("to_table") or "", re.I)}
    home_attrs = [f"{t['name']}.{a}" for t in record["tables"] for a in t["attributes"] if re.search(r"home_?store", a, re.I)]
    check("store_roles_distinguished", len(into_store) >= 2 or bool(home_attrs),
          f"relationships into the store dimension: {sorted(map(str, into_store))}; home store attributes: {home_attrs}")

    # Shipping is charged per order: it belongs at order grain. On a line-grain table it is
    # either repeated on every line (double counted) or allocated without an agreed rule.
    line_facts = [t for t in facts if LINE_GRAIN.search(t.get("grain") or "")]
    on_lines = [f"{t['name']}.{a}" for t in line_facts for a in t["attributes"] if HEADER_MEASURE.search(a)]
    check("no_order_measures_on_lines", not on_lines, f"shipping/tax attributes at line grain: {on_lines}")
    order_facts = [t["name"] for t in facts if ORDER_GRAIN.search(t.get("grain") or "")
                   and not LINE_GRAIN.search(t.get("grain") or "")
                   and any(re.search(r"shipping", a, re.I) for a in t["attributes"])]
    check("order_grain_fact_for_order_measures", bool(order_facts), f"order-grain facts with shipping: {order_facts}")

    for name, phrase, wanted in (
        ("order_value_feasible", "Average order value", FEASIBLE),
        ("shipping_by_category_honest", "Monthly shipping charged by product category", HONEST_PROFIT),
        ("refund_rate_feasible", "Monthly refund rate", FEASIBLE),
        ("sales_by_both_stores_feasible", "Monthly net sales by product category", FEASIBLE),
    ):
        r = _requirement(record, phrase)
        check(name, r is not None and r["feasibility"] in wanted, f"feasibility {r['feasibility'] if r else 'missing'}")

    returns_facts = [t["name"] for t in facts if re.search(r"refund|return", f"{t['name']} {t.get('grain') or ''}", re.I)]
    check("returns_fact_separate", bool(returns_facts) and len(facts) > len(returns_facts), f"returns facts: {returns_facts}")

    # Guest checkouts have no customer, so no home store: the design must say what happens.
    prose = _texts(record, "decisions", "requirements", "questions", "tables")
    check("guest_checkouts_handled", bool(re.search(r"guest", prose, re.I)),
          "guest checkouts addressed" if re.search(r"guest", prose, re.I) else "not addressed")
    return checks


GRADERS = {"northstar": grade_northstar, "northstar_structures": grade_structures}
