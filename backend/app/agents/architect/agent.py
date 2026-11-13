"""The Architect agent as an explicit LangGraph workflow.

    START -> model --(tool calls)--> tools --> model ... --> finalize -> END

- One run designs one candidate for the conversation's model (spec §4 phase 1). The model
  investigates the linked sources and edits the candidate workspace through tools.
- finalize recomputes the internal checks and settles the candidate: applied automatically
  as one changeset when the model is still empty, otherwise left `proposed` for the user to
  review as a diff and apply. A candidate with failed hard checks is still applied or
  proposed (the best partial draft) and the model stays Draft with its blockers listed.
  P4c inserts the Validator review and correction rounds before finalize.
"""

import traceback
import uuid
from typing import Any

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.embeddings import Embedder
from app.adapters.llm import ChatModel, LLMError
from app.adapters.sources import SourceConnector
from app.adapters.storage import ObjectStore
from app.agents.architect.prompt import SYSTEM_PROMPT
from app.agents.architect.tools import HANDLERS, OWN_TRANSACTION_TOOLS, TOOL_SPECS, WRITE_TOOLS, ArchitectContext
from app.agents.requirements.agent import _history
from app.agents.runtime import Emit, Loop, State, finish_run, last_text, record_usage, stop_outcome
from app.domain import design, modeling, sources, tenancy
from app.domain.access import Actor, ActorKind
from app.domain.records import RecordType
from app.persistence.conversations import AgentRun, Conversation, ConversationMessage, MessageRole, RunStatus
from app.persistence.design import CandidateStatus, DesignCandidate
from app.persistence.modeling import DataModel
from app.persistence.models import Project

AGENT_NAME = "architect"


def _context_block(session: Session, scope, model: DataModel, ctx: ArchitectContext, empty: bool) -> str:
    project = session.get(Project, scope.project_id)
    lines = [f"Project: {project.name}" + (f" — {project.description}" if project.description else ""),
             f"Data model: {model.name}" + (f" — {model.description}" if model.description else "")]
    lines.append("The model is empty; your candidate will be applied when you finish." if empty else
                 "The model already has content; your candidate starts as a copy of it and will be shown to the "
                 "user as a diff to apply.")
    linked = [sources.get_connection(session, scope, cid) for cid in ctx.source_ids]
    if linked:
        lines.append("Linked sources: " + ", ".join(f"{c.name} ({c.database}, source_id {c.id})" for c in linked))
    else:
        lines.append("Linked sources: none. Design from the registers only and say the design is unverified against data.")
    seq = modeling.current_seq(session, scope)
    counts = {name: len(modeling._latest(session, scope, rtype, as_of=seq))
              for name, rtype in [("requirements", RecordType.REQUIREMENT), ("definitions", RecordType.DEFINITION),
                                  ("questions", RecordType.QUESTION), ("decisions", RecordType.DECISION)]}
    lines.append("Registers: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    return "\n".join(lines)


def run_design(
    session_factory: sessionmaker[Session],
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    chat_model: ChatModel,
    embedder: Embedder,
    max_iterations: int,
    emit: Emit,
    connector: SourceConnector | None,
    box: sources.SecretBox | None,
    checkpointer: Any = None,
    store: ObjectStore | None = None,
    requirements_model: ChatModel | None = None,
    max_consultations: int = 3,
    consultation_max_iterations: int = 8,
) -> None:
    """Execute one Architect run. `requirements_model` drives the Business Requirements
    subgraph behind ask_requirements; without it the Architect can only raise questions. Never raises: outcomes are recorded on the run, the
    candidate and the assistant message, and reported through `emit`."""
    with session_factory() as session:
        run = session.get(AgentRun, run_id)
        conversation = session.get(Conversation, run.conversation_id)
        user_msg = session.scalar(
            select(ConversationMessage).where(
                ConversationMessage.conversation_id == conversation.id, ConversationMessage.run_id == run.id,
                ConversationMessage.role == MessageRole.USER))
        agent = Actor(kind=ActorKind.AGENT, id=run.id, on_behalf_of=run.initiated_by)
        try:
            scope = tenancy.resolve_project_scope(session, agent, tenant_id, project_id)
            model = modeling.get_model(session, scope, conversation.model_id)
        except Exception:  # membership revoked, or the model was deleted
            finish_run(session, run, None, RunStatus.FAILED, "you no longer have access to this model", emit, [])
            return

        # A new design supersedes earlier unapplied candidates for this model.
        for old in session.scalars(select(DesignCandidate).where(
                DesignCandidate.project_id == scope.project_id, DesignCandidate.model_id == model.id,
                DesignCandidate.status.in_([CandidateStatus.BUILDING, CandidateStatus.PROPOSED]))):
            old.status = CandidateStatus.DISCARDED
        candidate = design.start_candidate(session, scope, model.id, run.id)
        empty_at_start = design.model_is_empty(session, scope, model.id)
        session.commit()

        ctx = ArchitectContext(
            session=session, scope=scope, embedder=embedder, store=store,
            reason=f"Architect run {run.id}", model_id=model.id, candidate=candidate,
            connector=connector, box=box,
            source_ids=[c.id for c in sources.model_sources(session, scope, model.id) if not c.disabled_at],
            run=run, requirements_model=requirements_model, emit=emit, max_consultations=max_consultations,
            consultation_max_iterations=consultation_max_iterations,
        )
        emit({"type": "candidate", "candidate_id": str(candidate.id), "status": str(candidate.status)})

        transcript = _history(session, conversation.id, user_msg.ordinal)
        transcript.append({
            "role": "user",
            "text": f"<project_context>\n{_context_block(session, scope, model, ctx, empty_at_start)}\n"
                    f"</project_context>\n\n{user_msg.text}",
        })

        loop = Loop(session=session, run=run, chat_model=chat_model, system_prompt=SYSTEM_PROMPT,
                    tools=TOOL_SPECS, handlers=HANDLERS, write_tools=WRITE_TOOLS, ctx=ctx, emit=emit,
                    max_iterations=max_iterations, own_transaction=OWN_TRANSACTION_TOOLS)
        outcome: dict[str, Any] = {}

        def finalize(state: State) -> dict:
            outcome.update(_settle(session, scope, candidate, refused=state.get("stop") == "refusal"))
            return {}

        graph = StateGraph(State)
        loop.add_to(graph, after_loop="finalize")
        graph.add_node("finalize", finalize)
        graph.add_edge(START, "model")
        graph.add_edge("finalize", END)
        app = graph.compile(checkpointer=checkpointer)

        try:
            final = app.invoke(
                {"transcript": transcript, "iterations": 0, "stop": None},
                {"configurable": {"thread_id": str(run.id)}, "recursion_limit": 4 * max_iterations + 10},
            )
        except Exception as exc:
            session.rollback()
            if isinstance(exc, LLMError):
                detail = str(exc)
            else:
                traceback.print_exc()
                detail = f"the agent failed unexpectedly ({type(exc).__name__})"
            candidate.status = CandidateStatus.FAILED
            record_usage(run, loop.usage, ctx.changeset_seqs)
            finish_run(session, run, conversation, RunStatus.FAILED, detail, emit, loop.activity,
                       extra={"candidate": _candidate_event(candidate)})
            return
        finally:
            ctx.exits.close()  # close source sessions (MCP server processes)

        if outcome.get("applied_seq"):
            ctx.changeset_seqs.append(outcome["applied_seq"])
        record_usage(run, loop.usage, ctx.changeset_seqs)
        text = last_text(final["transcript"])
        note = outcome.get("note")
        stop = final.get("stop")
        status, detail = RunStatus.COMPLETED, None
        if stop:
            detail, text = stop_outcome(stop, text)
            status = RunStatus.STOPPED
        if note:
            text = f"{text}\n\n{note}".strip()
        finish_run(session, run, conversation, status, detail, emit, loop.activity, text=text,
                   extra={"candidate": _candidate_event(candidate)})


def _settle(session: Session, scope, candidate: DesignCandidate, *, refused: bool) -> dict[str, Any]:
    """Recompute checks, then apply or propose the candidate. Returns a note for the reply."""
    session.refresh(candidate)
    ctx_requirements = modeling._latest(session, scope, RecordType.REQUIREMENT, as_of=modeling.current_seq(session, scope))
    candidate.internal_checks = design.internal_checks(candidate.content, ctx_requirements)
    blockers = design.hard_failures(candidate.internal_checks)
    blocker_note = ("\n\nBlocking issues remain, so the model stays a Draft:\n" +
                    "\n".join(f"- {b['summary']}" for b in blockers)) if blockers else ""
    if refused or not candidate.content["tables"]:
        design.finish_candidate(candidate, CandidateStatus.FAILED)
        session.commit()
        return {"note": None if refused else "_No design was produced, so nothing was applied._"}
    if design.model_is_empty(session, scope, candidate.model_id) and not design.is_stale(session, scope, candidate):
        try:
            design.apply_candidate(session, scope, candidate.id)
        except Exception as exc:  # e.g. a payload the model got wrong; keep it for review instead
            session.rollback()
            session.refresh(candidate)
            design.finish_candidate(candidate, CandidateStatus.PROPOSED)
            session.commit()
            return {"note": f"_The design couldn't be applied automatically ({exc}); it is saved for review._"}
        session.commit()
        return {"applied_seq": candidate.applied_seq,
                "note": f"_Applied to the model as changeset {candidate.applied_seq}._{blocker_note}"}
    design.finish_candidate(candidate, CandidateStatus.PROPOSED)
    session.commit()
    return {"note": "_The model already had content, so these changes are proposed: review the diff and apply "
                    f"them._{blocker_note}"}


def _candidate_event(candidate: DesignCandidate) -> dict[str, Any]:
    return {"id": str(candidate.id), "status": str(candidate.status), "applied_seq": candidate.applied_seq,
            "hard_failures": len(design.hard_failures(candidate.internal_checks or []))}
