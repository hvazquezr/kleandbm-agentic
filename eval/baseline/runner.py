"""Run the Architect on a scenario with and without selective Tree of Thought, collect what
each run left behind, grade it and summarize the comparison (spec §7, P5).

Each run is a fresh tenant and project, so runs never see each other. The only difference
between variants is KLEANDBM_ARCHITECT_TOT_ENABLED: `baseline` removes open_decision_point,
giving the single-candidate Architect; `tot` keeps it. Everything else (models, prompts,
Validator, budgets) is the configured default.
"""

import json
import statistics
import time
from typing import Any, Callable

from grade import grade
from scenario import Scenario, set_up

VARIANTS = ("baseline", "tot")


def run_once(user, scenario: Scenario, variant: str, password: str, settings, *,
             fresh_user: Callable[[], Any] | None = None) -> dict[str, Any]:
    """One Architect run of the scenario under one variant. Returns the graded run record."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; use one of {VARIANTS}")
    ids = set_up(user, scenario, password)
    previous = settings.architect_tot_enabled
    settings.architect_tot_enabled = variant == "tot"
    started = time.monotonic()
    try:
        resp = user.post(f"{ids['base']}/conversations/{ids['conversation']}/messages", json={"text": scenario.prompt})
    finally:
        settings.architect_tot_enabled = previous
    seconds = time.monotonic() - started
    events = [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]
    done = events[-1] if events and events[-1].get("type") == "done" else {"status": "failed", "detail": resp.text[:500]}
    record = collect(fresh_user() if fresh_user else user, ids, done, seconds)
    record.update(variant=variant, scenario=scenario.name,
                  tools=[f"{e['status']}: {e['summary']}" for e in events if e.get("type") == "tool" and e.get("status") != "started"])
    record["grade"] = grade(record)
    return record


def collect(user, ids: dict[str, Any], done: dict[str, Any], seconds: float) -> dict[str, Any]:
    """Everything the comparison needs, read back through the API as a person would see it."""
    base = ids["base"]
    snap = user.get(f"{base}/models/{ids['model']}/snapshot").json()
    tables = []
    for t in snap["tables"]:
        tables.append({"name": t["data"]["name"], "kind": t["data"]["kind"], "grain": t["data"].get("grain"),
                       "source_ref": t["data"].get("source_ref"), "description": t["data"].get("description"),
                       "attributes": [a["data"]["name"] for a in snap["attributes"] if a["data"]["table_id"] == t["id"]]})
    record: dict[str, Any] = {
        "ids": ids, "status": done.get("status"), "detail": done.get("detail"), "seconds": round(seconds, 1),
        "state": snap["state"]["effective_state"], "tables": tables,
        "relationships": [_relationship(r["data"], snap) for r in snap["relationships"]],
        "requirements": [{"code": r["code"], "statement": r["data"]["statement"],
                          "feasibility": r["data"].get("feasibility_status"), "notes": r["data"].get("feasibility_notes")}
                         for r in snap["requirements"]],
        "questions": [{"code": q["code"], "kind": q["data"]["kind"], "description": q["data"]["description"],
                       "status": q["data"]["status"], "is_blocking": q["data"]["is_blocking"]} for q in snap["questions"]],
        "decisions": [{"code": d["code"], "kind": d["data"]["kind"], "statement": d["data"]["statement"],
                       "rationale": d["data"].get("rationale"), "status": d["data"]["status"]} for d in snap["decisions"]],
        "definitions": [{"code": d["code"], "term": d["data"]["term"], "provenance": d["data"]["provenance"],
                         "confirmation": d["data"]["confirmation_status"]} for d in snap["definitions"]],
        "candidate": None, "decision_points": [], "usage": {},
    }
    cand_id = (done.get("candidate") or {}).get("id")
    if cand_id:
        cand = user.get(f"{base}/candidates/{cand_id}").json()
        rounds = cand.get("validation") or []
        last = next((r for r in reversed(rounds) if r.get("status") == "completed"), None)
        counts = {s: sum(1 for f in (last or {}).get("findings", []) if f["status"] == s)
                  for s in ("passed", "failed", "unverified")}
        record["candidate"] = {
            "id": cand_id, "status": cand["status"], "ready_for_review": cand["ready_for_review"],
            "blockers": cand["blockers"], "hard_failures": cand["hard_failures"],
            "review_rounds": len(rounds), "reviewed": last is not None, "findings": counts,
            "correction_attempts": cand.get("issue_attempts") or {},
        }
        for dp in user.get(f"{base}/candidates/{cand_id}/decision-points").json():
            record["decision_points"].append({
                "trigger": dp["trigger"], "question": dp["question"], "status": dp["status"],
                "stop_reason": dp["stop_reason"], "usage": dp["usage"],
                "nodes": [{"depth": n["depth"], "stage": n["stage"], "status": n["status"], "choice": n["choice"],
                           "total": (n["evaluation"] or {}).get("total"), "reason": n["reason"]} for n in dp["nodes"]],
            })
    run_id = (done.get("run") or {}).get("id")
    if run_id:
        record["usage"] = usage(user, base, run_id)
    return record


def _relationship(data: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    tables = {t["id"]: t["data"]["name"] for t in snap["tables"]}
    attrs = {a["id"]: a["data"]["name"] for a in snap["attributes"]}
    return {"from_table": tables.get(data["from_table_id"]), "from_attribute": attrs.get(data["from_attribute_id"]),
            "to_table": tables.get(data["to_table_id"]), "to_attribute": attrs.get(data["to_attribute_id"]),
            "role_name": data.get("role_name")}


TOKENS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def usage(user, base: str, run_id: str) -> dict[str, Any]:
    """Usage per agent (the Architect run, including its searches, plus its consultation and
    Validator child runs) and in total."""
    by_agent: dict[str, dict[str, Any]] = {}
    pending = [run_id]
    while pending:
        run = user.get(f"{base}/agent-runs/{pending.pop()}").json()
        a = by_agent.setdefault(run["agent"], {"runs": 0, "model_calls": 0, "tool_calls": 0, "llm_model": run["llm_model"],
                                               **{k: 0 for k in TOKENS}})
        a["runs"] += 1
        a["model_calls"] += run["iterations"]
        a["tool_calls"] += run["tool_calls"]
        for k in TOKENS:
            a[k] += run[k] or 0
        pending += [str(c) for c in run["child_run_ids"]]
    total = {k: sum(a[k] for a in by_agent.values()) for k in ("model_calls", "tool_calls", *TOKENS)}
    return {"by_agent": by_agent, "total": total}


def cost(record: dict[str, Any], prices: dict[str, dict[str, float]] | None) -> float | None:
    """Dollar cost from a prices file ({llm_model: {input, output, cache_read, cache_write}} in
    dollars per million tokens). None without prices for every model used: no guessed prices."""
    if not prices or not record.get("usage"):
        return None
    dollars = 0.0
    for a in record["usage"]["by_agent"].values():
        p = prices.get(a["llm_model"])
        if p is None:
            return None
        dollars += sum(a[f"{k}_tokens"] * p.get(k, 0.0) for k in ("input", "output", "cache_read", "cache_write")) / 1e6
    return round(dollars, 4)


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 2) if values else None


def summarize(records: list[dict[str, Any]], prices: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for variant in VARIANTS:
        rs = [r for r in records if r["variant"] == variant]
        if not rs:
            continue
        checks = sorted({c for r in rs for c in r["grade"]})
        cands = [r["candidate"] for r in rs if r["candidate"]]
        costs = [c for c in (cost(r, prices) for r in rs) if c is not None]
        dps = [dp for r in rs for dp in r["decision_points"]]
        out[variant] = {
            "runs": len(rs),
            "completed": sum(1 for r in rs if r["status"] == "completed"),
            "rubric": {c: sum(1 for r in rs if r["grade"].get(c, {}).get("passed")) for c in checks},
            "rubric_score": _mean([sum(g["passed"] for g in r["grade"].values()) / len(r["grade"]) for r in rs]),
            "ready_for_review": sum(1 for c in cands if c["ready_for_review"]),
            "blockers": _mean([len(c["blockers"]) for c in cands]),
            "findings": {s: _mean([c["findings"][s] for c in cands if c["reviewed"]]) for s in ("passed", "failed", "unverified")},
            "review_rounds": _mean([c["review_rounds"] for c in cands]),
            "tables": _mean([len(r["tables"]) for r in rs]),
            "questions": _mean([len(r["questions"]) for r in rs]),
            "decision_points": len(dps),
            "search_outcomes": {s: sum(1 for d in dps if d["status"] == s) for s in sorted({d["status"] for d in dps})},
            "seconds": _mean([r["seconds"] for r in rs]),
            "model_calls": _mean([r["usage"]["total"]["model_calls"] for r in rs if r.get("usage")]),
            "output_tokens": _mean([r["usage"]["total"]["output_tokens"] for r in rs if r.get("usage")]),
            "input_tokens": _mean([r["usage"]["total"]["input_tokens"] for r in rs if r.get("usage")]),
            "search_output_tokens": _mean([sum(d["usage"].get("output_tokens", 0) for d in r["decision_points"]) for r in rs]),
            "cost_usd": _mean(costs) if len(costs) == len(rs) else None,
        }
    return out


def report(summary: dict[str, Any], records: list[dict[str, Any]]) -> str:
    """A Markdown comparison: one column per variant, means over its runs."""
    variants = [v for v in VARIANTS if v in summary]
    names = sorted({r["scenario"] for r in records})
    lines = [f"# Baseline comparison: single candidate vs. selective ToT ({', '.join(names)})", ""]
    lines += [f"Runs per variant: {', '.join(f'{v} {summary[v]['runs']}' for v in variants)}. "
              "Values are means over runs; rubric rows count the runs that passed. Rubric checks are "
              "keyword heuristics over the records (eval/baseline/grade.py), so check surprising rows in the run JSON.", ""]
    head = "| | " + " | ".join(variants) + " |"
    lines += [head, "|---" * (len(variants) + 1) + "|"]

    def row(label: str, fn) -> None:
        lines.append(f"| {label} | " + " | ".join(str(fn(summary[v])) for v in variants) + " |")

    row("completed runs", lambda s: f"{s['completed']}/{s['runs']}")
    row("**rubric score** (share of checks passed)", lambda s: s["rubric_score"])
    for check in sorted({c for v in variants for c in summary[v]["rubric"]}):
        row(f"&nbsp;&nbsp;{check}", lambda s, c=check: f"{s['rubric'].get(c, 0)}/{s['runs']}")
    row("Ready for Review", lambda s: f"{s['ready_for_review']}/{s['runs']}")
    row("readiness blockers", lambda s: s["blockers"])
    row("Validator passed / failed / unverified",
        lambda s: " / ".join(str(s["findings"][k]) for k in ("passed", "failed", "unverified")))
    row("review rounds", lambda s: s["review_rounds"])
    row("tables", lambda s: s["tables"])
    row("questions raised", lambda s: s["questions"])
    row("decision points (total)", lambda s: s["decision_points"])
    row("search outcomes", lambda s: ", ".join(f"{k} {n}" for k, n in s["search_outcomes"].items()) or "–")
    row("seconds", lambda s: s["seconds"])
    row("model calls (all agents)", lambda s: s["model_calls"])
    row("input tokens", lambda s: s["input_tokens"])
    row("output tokens", lambda s: s["output_tokens"])
    row("&nbsp;&nbsp;of which in searches", lambda s: s["search_output_tokens"])
    row("cost (USD)", lambda s: s["cost_usd"] if s["cost_usd"] is not None else "– (pass --prices)")
    lines += ["", "## Runs", ""]
    for i, r in enumerate(records, 1):
        failed = [c for c, g in r["grade"].items() if not g["passed"]]
        dps = "; ".join(f"{d['trigger']} → {d['status']}" for d in r["decision_points"]) or "none"
        lines.append(f"{i}. **{r['variant']}** · {r['status']} · {r['state']} · {r['seconds']}s · "
                     f"decision points: {dps} · failed checks: {', '.join(failed) or 'none'}")
    return "\n".join(lines) + "\n"
