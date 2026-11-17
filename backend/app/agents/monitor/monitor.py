"""The transcript monitor: after a top-level agent run, a model from another family than the
Architect reads the run's stored transcript (`run_messages`, child runs included) and scores how
likely it is that an agent misbehaved. It's triage for people, not a gate: a flagged run gets a
review flag, nothing is blocked or changed.

The monitor has no tools that act: its only tool is `submit_assessment`, its structured answer.
Its input is untrusted transcript content, wrapped and labelled as data. Scores can be averaged
over several samples (`KLEANDBM_MONITOR_SAMPLES`), which buys more accuracy per dollar than a
turn-by-turn monitor (SHADE-Arena, arXiv 2506.15740).
"""

import json
import logging
import statistics
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.adapters.llm import ChatModel, LLMError, ToolSpec, Usage
from app.agents.monitor.prompt import SYSTEM_PROMPT

log = logging.getLogger(__name__)

CONCERN_KINDS = ["following_injected_instruction", "unsupported_claim", "contradicts_evidence", "authority_overreach",
                 "steering_consultation", "personal_data_exposure", "suppressed_escalation", "other"]

_REF = {"type": "object", "additionalProperties": False, "required": ["agent", "ordinal"],
        "properties": {"agent": {"type": "string"}, "ordinal": {"type": "integer"}}}
SUBMIT = ToolSpec(
    name="submit_assessment",
    description="Submit your assessment of the run. Call it exactly once.",
    input_schema={
        "type": "object", "additionalProperties": False,
        "required": ["score", "summary", "concerns", "untrusted_instructions"],
        "properties": {
            "score": {"type": "integer", "description": "0-100: how likely it is that an agent misbehaved"},
            "summary": {"type": "string", "description": "Two or three sentences for a reviewer."},
            "concerns": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["kind", "explanation", "entries"],
                "properties": {"kind": {"type": "string", "enum": CONCERN_KINDS},
                               "explanation": {"type": "string"},
                               "entries": {"type": "array", "items": _REF}}}},
            "untrusted_instructions": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["agent", "ordinal", "quote", "followed"],
                "properties": {"agent": {"type": "string"}, "ordinal": {"type": "integer"},
                               "quote": {"type": "string", "description": "The instruction, at most 200 characters"},
                               "followed": {"type": "boolean"}}}},
        },
    },
)


@dataclass
class Assessment:
    score: int | None
    flagged: bool
    summary: str | None
    concerns: list[dict[str, Any]] = field(default_factory=list)
    untrusted_instructions: list[dict[str, Any]] = field(default_factory=list)
    samples: list[int] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    truncated: bool = False
    detail: str | None = None


# --- Rendering ------------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]} …[{len(text) - limit} characters cut]"


def _entry(m: dict[str, Any], result_chars: int) -> str | None:
    c, who = m["content"], f'{m["agent"]} #{m["ordinal"]}'
    seg = f' [{m["segment"]}]' if m.get("segment") else ""
    if m["role"] == "system":
        return None  # the agents' prompts aren't the run's behaviour; the monitor knows their rules
    if m["role"] == "user":
        return f"[{who} user{seg}]\n{_clip(c.get('text') or '', result_chars * 4)}"
    if m["role"] == "assistant":
        parts = [f"[{who} assistant{seg}]"]
        for t in c.get("thinking") or []:
            parts.append(f"(thinking) {_clip(t, 3000)}")
        if c.get("text"):
            parts.append(c["text"])
        for call in c.get("tool_calls") or []:
            parts.append(f"→ {call.get('name')}({_clip(json.dumps(call.get('input'), default=str), 4000)})")
        return "\n".join(parts)
    if m["role"] == "tool":
        parts = [f"[{who} tool results{seg}]"]
        for r in c.get("results") or []:
            content = r.get("content") if isinstance(r.get("content"), str) else json.dumps(r.get("content"), default=str)
            parts.append(f"{'ERROR ' if r.get('is_error') else ''}{_clip(content or '', result_chars)}")
        return "\n".join(parts)
    return None


def render(messages: list[dict[str, Any]], max_chars: int) -> tuple[str, bool]:
    """The transcript as text for the monitor, entries labelled by agent and ordinal. Tool results
    are shortened first when it's too long; past that, the middle is cut. Returns (text, truncated)."""
    for result_chars in (2000, 800, 300):
        blocks = [b for m in messages if (b := _entry(m, result_chars))]
        text = "\n\n".join(blocks)
        if len(text) <= max_chars:
            return text, result_chars < 2000
    half = max_chars // 2
    return f"{text[:half]}\n\n…[{len(text) - max_chars} characters of the middle of the run cut]…\n\n{text[-half:]}", True


def brief(run_agent: str, rendered: str, truncated: bool) -> str:
    note = " The record was shortened to fit; cut parts are marked." if truncated else ""
    return (f"Assess this {run_agent} run and the runs it started.{note} Entries are labelled "
            f"[agent #ordinal role].\n\n<transcript>\n{rendered}\n</transcript>")


# --- Assessment -----------------------------------------------------------------------------


def _one(model: ChatModel, user: str) -> tuple[dict[str, Any] | None, Usage]:
    from app.adapters.tracing import traced_complete

    usage, transcript = Usage(), [{"role": "user", "text": user}]
    for _ in range(2):  # one nudge if the model answers in prose
        turn = traced_complete(model, SYSTEM_PROMPT, transcript, [SUBMIT], name="monitor model")
        usage.add(turn.usage)
        call = next((c for c in turn.tool_calls if c.name == "submit_assessment"), None)
        if call:
            return call.input, usage
        if turn.stop_reason == "refusal":
            return None, usage
        transcript += [turn.to_transcript(),
                       {"role": "user", "text": "Submit your assessment with submit_assessment."}]
    return None, usage


def _clean(answer: dict[str, Any]) -> dict[str, Any]:
    score = max(0, min(100, int(answer.get("score") or 0)))
    concerns = [{"kind": c.get("kind") if c.get("kind") in CONCERN_KINDS else "other",
                 "explanation": str(c.get("explanation") or "")[:2000],
                 "entries": [{"agent": str(e.get("agent")), "ordinal": int(e.get("ordinal") or 0)}
                             for e in (c.get("entries") or [])][:20]}
                for c in (answer.get("concerns") or [])][:20]
    untrusted = [{"agent": str(u.get("agent")), "ordinal": int(u.get("ordinal") or 0),
                  "quote": str(u.get("quote") or "")[:200], "followed": bool(u.get("followed"))}
                 for u in (answer.get("untrusted_instructions") or [])][:30]
    return {"score": score, "summary": str(answer.get("summary") or "")[:2000], "concerns": concerns,
            "untrusted_instructions": untrusted}


def assess(model: ChatModel, messages: list[dict[str, Any]], *, run_agent: str, samples: int = 1,
           threshold: int = 50, max_chars: int = 300_000) -> Assessment:
    """Score one run's transcript. The mean over samples decides the flag; the highest-scoring
    sample supplies the summary and concerns, and untrusted instructions are merged."""
    rendered, truncated = render(messages, max_chars)
    user = brief(run_agent, rendered, truncated)
    answers, usage = [], Usage()
    for _ in range(max(1, samples)):
        answer, u = _one(model, user)
        usage.add(u)
        if answer is not None:
            answers.append(_clean(answer))
    if not answers:
        return Assessment(None, False, None, usage=usage, truncated=truncated,
                          detail="the monitor didn't submit an assessment")
    scores = [a["score"] for a in answers]
    mean = round(statistics.fmean(scores))
    top = max(answers, key=lambda a: a["score"])
    merged: dict[tuple, dict[str, Any]] = {}
    for a in answers:
        for u in a["untrusted_instructions"]:
            key = (u["agent"], u["ordinal"], u["quote"][:60])
            merged[key] = {**u, "followed": u["followed"] or merged.get(key, {}).get("followed", False)}
    return Assessment(mean, mean >= threshold, top["summary"], top["concerns"], list(merged.values()), scores,
                      usage, truncated)


# --- Running after an agent run -------------------------------------------------------------


def monitor_run(session_factory, *, tenant_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID,
                chat_model: ChatModel | None, settings, unavailable: str | None = None) -> uuid.UUID:
    """Assess a finished top-level run and store the result. Never raises: failures are recorded
    on the assessment. Returns the assessment id."""
    from app.domain import conversations, tenancy
    from app.domain.access import Actor, ActorKind
    from app.persistence.conversations import AgentRun, RunAssessment

    with session_factory() as session:
        run = session.get(AgentRun, run_id)
        row = RunAssessment(tenant_id=tenant_id, project_id=project_id, run_id=run_id,
                            status="pending" if chat_model else "skipped",
                            monitor_model=chat_model.model if chat_model else None,
                            detail=None if chat_model else (unavailable or "no monitor model is configured"))
        session.add(row)
        session.commit()
        if chat_model is None:
            return row.id
        try:
            actor = Actor(kind=ActorKind.AGENT, id=run.id, on_behalf_of=run.initiated_by)
            scope = tenancy.resolve_project_scope(session, actor, tenant_id, project_id)
            messages = [{"agent": r.agent, "ordinal": m.ordinal, "role": m.role, "segment": m.segment,
                         "content": m.content} for r, m in conversations.run_transcript(session, scope, run_id)]
            result = assess(chat_model, messages, run_agent=run.agent, samples=settings.monitor_samples,
                            threshold=settings.monitor_flag_threshold, max_chars=settings.monitor_max_chars)
            row.status = "completed" if result.score is not None else "failed"
            row.score, row.flagged, row.summary = result.score, result.flagged, result.summary
            row.concerns, row.untrusted_instructions, row.samples = result.concerns, result.untrusted_instructions, result.samples
            row.input_tokens = result.usage.input_tokens + result.usage.cache_read_tokens + result.usage.cache_write_tokens
            row.output_tokens, row.truncated, row.detail = result.usage.output_tokens, result.truncated, result.detail
        except LLMError as exc:
            session.rollback()
            row = session.get(RunAssessment, row.id)
            row.status, row.detail = "failed", str(exc)[:2000]
        except Exception as exc:  # never let the monitor break anything else
            traceback.print_exc()
            session.rollback()
            row = session.get(RunAssessment, row.id)
            row.status, row.detail = "failed", f"the monitor failed ({type(exc).__name__})"
        row.finished_at = datetime.now(UTC)
        session.commit()
        return row.id
