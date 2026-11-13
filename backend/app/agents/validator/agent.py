"""The Validator agent as a LangGraph subgraph the Architect invokes (spec §7).

    START -> model --(tool calls)--> tools --> model ... --(submit_review)--> END

One call reviews one round: round 0 is the first review of the candidate; later rounds
revalidate it after an Architect correction, re-checking earlier findings under their issue
keys. Each round is its own AgentRun (agent "validator", parent = the Architect's run), on a
different model family than the Architect, with read-only tools. It returns the round; it
never edits the candidate, the model or the registers.
"""

import traceback
from datetime import UTC, datetime
from typing import Any

from langgraph.graph import START, StateGraph
from sqlalchemy.orm import Session

from app.adapters.embeddings import Embedder
from app.adapters.llm import ChatModel, LLMError
from app.adapters.sources import SourceConnector
from app.adapters.storage import ObjectStore
from app.agents.runtime import Emit, Loop, State, record_usage
from app.agents.validator.prompt import SYSTEM_PROMPT
from app.agents.validator.tools import HANDLERS, TOOL_SPECS, WRITE_TOOLS, ValidatorContext
from app.domain import design, sources, tenancy
from app.domain.access import Actor, ActorKind
from app.persistence.conversations import AgentRun, RunStatus
from app.persistence.design import DesignCandidate

AGENT_NAME = "validator"


def _brief(session: Session, scope, candidate: DesignCandidate, source_ids, round_no: int,
           previous: list[dict[str, Any]] | None, correction: str | None) -> str:
    lines = ["<review_brief>",
             f"Candidate {candidate.id}, built from model watermark {candidate.base_seq}. "
             f"Review round {round_no} ({'first review' if round_no == 0 else 'revalidation after a correction'})."]
    linked = [sources.get_connection(session, scope, cid) for cid in source_ids]
    lines.append("Linked sources: " + (", ".join(f"{c.name} ({c.database})" for c in linked) if linked else
                                       "none, so source-dependent checks can only be unverified."))
    if previous:
        lines.append("Earlier findings to re-check (reuse these issue keys):")
        lines += [f"- {f['issue_key']} [{f['status']}{', blocking' if f.get('is_blocking') else ''}]: {f['summary']}"
                  for f in previous]
    if correction:
        lines.append(f"The Architect's account of its correction (a claim to verify, not evidence): {correction[:3000]}")
    lines.append("</review_brief>")
    return "\n".join(lines)


def review(
    session: Session,
    *,
    parent: AgentRun,
    candidate: DesignCandidate,
    chat_model: ChatModel,
    embedder: Embedder,
    store: ObjectStore | None,
    connector: SourceConnector | None,
    box: sources.SecretBox | None,
    source_ids: list,
    round_no: int,
    previous: list[dict[str, Any]] | None,
    correction: str | None,
    max_iterations: int,
    emit: Emit,
) -> dict[str, Any]:
    """Run one review round. Never raises: a provider failure gives a round with status
    "failed", which leaves the design unverified."""
    child = AgentRun(
        tenant_id=parent.tenant_id, project_id=parent.project_id, conversation_id=parent.conversation_id,
        agent=AGENT_NAME, parent_run_id=parent.id, llm_model=chat_model.model, status=RunStatus.RUNNING,
        initiated_by=parent.initiated_by, changeset_seqs=[],
    )
    session.add(child)
    session.commit()
    actor = Actor(kind=ActorKind.AGENT, id=child.id, on_behalf_of=child.initiated_by)
    scope = tenancy.resolve_project_scope(session, actor, parent.tenant_id, parent.project_id)
    ctx = ValidatorContext(
        session=session, scope=scope, embedder=embedder, store=store,
        reason=f"Validator run {child.id} (round {round_no} for Architect run {parent.id})",
        model_id=candidate.model_id, candidate=candidate, connector=connector, box=box, source_ids=list(source_ids),
        user_edited=design.user_edited_elements(session, scope, candidate),
    )

    def forward(event: dict[str, Any]) -> None:
        if event.get("type") == "tool":  # its tool activity shows in the stream; its text doesn't
            emit({**event, "agent": AGENT_NAME})

    loop = Loop(session=session, run=child, chat_model=chat_model, system_prompt=SYSTEM_PROMPT, tools=TOOL_SPECS,
                handlers=HANDLERS, write_tools=WRITE_TOOLS, ctx=ctx, emit=forward, max_iterations=max_iterations,
                finish_tools={"submit_review"})
    graph = StateGraph(State)
    loop.add_to(graph)
    graph.add_edge(START, "model")
    app = graph.compile()

    status, detail = "completed", None
    try:
        brief = _brief(session, scope, candidate, source_ids, round_no, previous, correction)
        final = app.invoke({"transcript": [{"role": "user", "text": brief}], "iterations": 0, "stop": None},
                           {"recursion_limit": 4 * max_iterations + 10})
        if ctx.summary is None:
            status, detail = "incomplete", final.get("stop") or "the Validator ended without submitting its review"
    except Exception as exc:
        session.rollback()
        if not isinstance(exc, LLMError):
            traceback.print_exc()
        status, detail = "failed", str(exc) if isinstance(exc, LLMError) else f"failed unexpectedly ({type(exc).__name__})"
    finally:
        ctx.exits.close()

    record_usage(child, loop.usage, [])
    child.status = {"completed": RunStatus.COMPLETED, "incomplete": RunStatus.STOPPED}.get(status, RunStatus.FAILED)
    child.status_detail, child.finished_at = detail, datetime.now(UTC)
    session.commit()
    return {"round": round_no, "run_id": str(child.id), "model": chat_model.model, "status": status,
            "detail": detail, "summary": ctx.summary, "findings": list(ctx.findings.values())}
