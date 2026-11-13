"""Rubric for one Northstar run, from what the run left in the model and registers.

The checks follow eval/northstar/README.md (the answer key, grader-side only). They are
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
    """{check name: {"passed": bool, "detail": str}}."""
    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, passed: bool, detail: str) -> None:
        checks[name] = {"passed": bool(passed), "detail": detail}

    facts = [t for t in record["tables"] if t["kind"] == "fact"]
    check("facts_declare_grain", facts and all(t.get("grain") for t in facts),
          f"{len(facts)} facts; without grain: {[t['name'] for t in facts if not t.get('grain')]}")

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
