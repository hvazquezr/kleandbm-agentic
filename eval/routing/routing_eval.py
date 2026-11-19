"""Routing accuracy (spec §8 request router; backlog step 6): the router's own model call on a
labelled set of user messages, with briefs built by the router's own formatters, so the eval
measures exactly what production sends. No database, documents or agents: one router call per
case and repeat.

A case is correct when the route is one of its `expect` routes; `strict` counts only the first
(the intended) route. A turn where the model gave no valid route is the router's clarify
fallback, counted as `fallback` and scored like any other clarify.
"""

import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.adapters.llm import ChatModel, LLMError
from app.agents.router.agent import AGENT_LABELS, ROUTES, brief, earlier_line, format_state, route_call

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases.json"
CLARIFY_LABEL = "the assistant (a question back)"  # what the router's history says for its own questions


def load(path: Path = CASES) -> dict[str, Any]:
    data = json.loads(path.read_text())
    check(data)
    return data


def check(data: dict[str, Any]) -> None:
    """Refuse a malformed set: unknown states, routes or agents, duplicate ids."""
    seen: set[str] = set()
    for case in data["cases"]:
        cid = case["id"]
        if cid in seen:
            raise ValueError(f"duplicate case id {cid}")
        seen.add(cid)
        if case["state"] not in data["states"]:
            raise ValueError(f"{cid}: unknown state {case['state']!r}")
        if not case["expect"] or any(r not in ROUTES for r in case["expect"]):
            raise ValueError(f"{cid}: expect must list routes from {ROUTES}")
        for _, agent in case.get("earlier", []):
            if agent is not None and agent not in AGENT_LABELS:
                raise ValueError(f"{cid}: unknown earlier agent {agent!r}")


def case_brief(data: dict[str, Any], case: dict[str, Any]) -> str:
    state = format_state(**data["states"][case["state"]])
    earlier = [earlier_line(text, AGENT_LABELS[agent] if agent else CLARIFY_LABEL)
               for text, agent in case.get("earlier", [])]
    return brief(state, earlier, case["message"])


def run_cases(model: ChatModel, data: dict[str, Any], repeats: int = 1, *, progress=None) -> list[dict[str, Any]]:
    results = []
    for repeat in range(1, repeats + 1):
        for case in data["cases"]:
            row: dict[str, Any] = {"id": case["id"], "repeat": repeat, "expect": case["expect"],
                                   "tags": case.get("tags", []), "message": case["message"]}
            started = time.monotonic()
            try:
                decision, turn = route_call(model, case_brief(data, case))
                row.update(route=decision["route"], reason=decision["reason"], question=decision.get("question"),
                           fallback=bool(decision.get("fallback")), model=turn.model,
                           input_tokens=turn.usage.input_tokens, output_tokens=turn.usage.output_tokens,
                           cache_read_tokens=turn.usage.cache_read_tokens)
            except LLMError as exc:
                row.update(route=None, error=str(exc), fallback=False)
            row["seconds"] = round(time.monotonic() - started, 2)
            row["correct"] = row["route"] in case["expect"]
            row["strict"] = row["route"] == case["expect"][0]
            results.append(row)
            if progress:
                progress(row)
    return results


def _pct(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


def summarize(results: list[dict[str, Any]], model_name: str | None = None) -> dict[str, Any]:
    n = len(results)
    by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_route[r["expect"][0]].append(r)
    confusion = {exp: dict(Counter(r["route"] or "error" for r in rows)) for exp, rows in sorted(by_route.items())}
    per_case: dict[str, set] = defaultdict(set)
    for r in results:
        per_case[r["id"]].add(r["route"])
    repeats = max((r["repeat"] for r in results), default=1)
    seconds = sorted(r["seconds"] for r in results if not r.get("error"))
    return {
        "model": model_name or next((r.get("model") for r in results if r.get("model")), None),
        "cases": len(per_case), "repeats": repeats, "calls": n,
        "accuracy": _pct(sum(r["correct"] for r in results), n),
        "strict_accuracy": _pct(sum(r["strict"] for r in results), n),
        "by_expected_route": {exp: {"calls": len(rows), "accuracy": _pct(sum(r["correct"] for r in rows), len(rows))}
                              for exp, rows in sorted(by_route.items())},
        "confusion": confusion,
        "consistent_cases": _pct(sum(1 for routes in per_case.values() if len(routes) == 1), len(per_case))
        if repeats > 1 else None,
        "fallbacks": sum(r["fallback"] for r in results),
        "errors": sum(1 for r in results if r.get("error")),
        "seconds": {"median": round(statistics.median(seconds), 2) if seconds else None,
                    "p95": seconds[max(0, int(0.95 * len(seconds)) - 1)] if seconds else None,
                    "max": seconds[-1] if seconds else None},
        "tokens": {k: sum(r.get(k) or 0 for r in results) for k in ("input_tokens", "output_tokens", "cache_read_tokens")},
    }


def report(summary: dict[str, Any], results: list[dict[str, Any]]) -> str:
    s = summary
    lines = [
        "# Router accuracy", "",
        f"Model `{s['model']}` · {s['cases']} cases × {s['repeats']} repeat(s) = {s['calls']} calls.", "",
        f"- **Accuracy:** {s['accuracy']}% (a route the case accepts); strict {s['strict_accuracy']}% (the intended route).",
    ]
    if s["consistent_cases"] is not None:
        lines.append(f"- **Consistency:** {s['consistent_cases']}% of cases got the same route on every repeat.")
    lines += [
        f"- **Fallbacks** (no valid route, became clarify): {s['fallbacks']} · **errors:** {s['errors']}.",
        f"- **Latency:** median {s['seconds']['median']} s, p95 {s['seconds']['p95']} s, max {s['seconds']['max']} s.",
        f"- **Tokens:** {s['tokens']['input_tokens']} in ({s['tokens']['cache_read_tokens']} from cache), "
        f"{s['tokens']['output_tokens']} out.", "",
        "## By intended route", "", "| Intended | Calls | Accuracy | Got |", "|---|---|---|---|",
    ]
    for exp, row in s["by_expected_route"].items():
        got = ", ".join(f"{k} {v}" for k, v in sorted(s["confusion"][exp].items(), key=lambda kv: -kv[1]))
        lines.append(f"| {exp} | {row['calls']} | {row['accuracy']}% | {got} |")
    misses = [r for r in results if not r["correct"]]
    lines += ["", f"## Misses ({len(misses)})", ""]
    if not misses:
        lines.append("None.")
    else:
        lines += ["| Case | Repeat | Expected | Got | Message | Router's reason |", "|---|---|---|---|---|---|"]
        for r in misses:
            cell = lambda t: (t or "").replace("|", "\\|").replace("\n", " ")  # noqa: E731
            lines.append(f"| {r['id']} | {r['repeat']} | {' or '.join(r['expect'])} | {r['route'] or 'error: ' + cell(r.get('error'))} "
                         f"| {cell(r['message'])} | {cell(r.get('reason'))} |")
    return "\n".join(lines) + "\n"
