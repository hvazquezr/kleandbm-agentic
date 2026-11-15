"""The Business Requirements agent as an explicit LangGraph workflow.

    START -> model --(tool calls)--> tools --> model ... --> END

One graph run answers one user message (one AgentRun). The graph state holds a neutral
transcript; LangGraph checkpoints it per run (thread_id = run id), separate from the
registers. The loop itself (budget, savepoints, streaming) is in app.agents.runtime.
"""

import traceback
import uuid
from typing import Any

from langgraph.graph import START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.embeddings import Embedder
from app.adapters.llm import ChatModel, LLMError
from app.adapters.storage import ObjectStore
from app.agents.requirements.prompt import SYSTEM_PROMPT
from app.agents.requirements.tools import HANDLERS, TOOL_SPECS, WRITE_TOOLS, ToolContext
from app.agents.runtime import Emit, Loop, State, finish_run, last_text, record_usage, stop_outcome
from app.domain import documents, modeling, sources, tenancy
from app.domain.access import Actor, ActorKind
from app.persistence.conversations import AgentRun, Conversation, ConversationMessage, MessageRole, RunStatus
from app.persistence.modeling import DataModel
from app.persistence.models import Project

AGENT_NAME = "business_requirements"
HISTORY_MESSAGES = 20


def _context_block(session: Session, scope, conversation: Conversation, attachments: list[str]) -> str:
    """Per-run project facts. Volatile, so it goes in the user turn, not the cached system prompt."""
    project = session.get(Project, scope.project_id)
    lines = [f"Project: {project.name}" + (f" — {project.description}" if project.description else "")]
    if conversation.model_id:
        model = session.get(DataModel, conversation.model_id)
        if model:
            lines.append(f"Data model: {model.name}" + (f" — {model.description}" if model.description else ""))
    lines.append(_sources_line(session, scope, conversation))
    docs = documents.list_documents(session, scope)
    if docs:
        lines.append("Documents:")
        for doc, v in docs:
            lines.append(f"- {doc.title} (v{v.version_no}, {v.filename}, {v.status}, version_id {v.id})")
    else:
        lines.append("Documents: none uploaded yet.")
    seq = modeling.current_seq(session, scope)
    counts = {
        name: len(modeling._latest(session, scope, rtype, as_of=seq))
        for name, rtype in [("requirements", modeling.RecordType.REQUIREMENT),
                            ("definitions", modeling.RecordType.DEFINITION),
                            ("questions", modeling.RecordType.QUESTION)]
    }
    lines.append("Registers: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    if attachments:
        lines.append("Attached to this message: " + ", ".join(attachments))
    return "\n".join(lines)


def _sources_line(session: Session, scope, conversation: Conversation) -> str:
    """Which sources exist, by name only (never host, user or credentials). This agent has no
    source tools; saying so stops it from guessing that none is connected."""
    if conversation.model_id:
        linked = [c for c in sources.model_sources(session, scope, conversation.model_id) if not c.disabled_at]
        label = "Data sources linked to this model"
    else:
        linked = [c for c in sources.list_connections(session, scope) if not c.disabled_at]
        label = "Data sources in this project"
    if not linked:
        return f"{label}: none yet."
    names = ", ".join(f"{c.name} (MySQL database {c.database})" for c in linked)
    return f"{label}: {names}. You can't query them; the Architect checks them when it designs."


def _history(session: Session, conversation_id: uuid.UUID, upto_ordinal: int) -> list[dict[str, Any]]:
    rows = list(session.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id, ConversationMessage.ordinal < upto_ordinal)
        .order_by(ConversationMessage.ordinal.desc())
        .limit(HISTORY_MESSAGES)
    ))
    out = []
    for m in reversed(rows):
        out.append({"role": "user", "text": m.text} if m.role is MessageRole.USER
                   else {"role": "assistant", "text": m.text, "tool_calls": [], "raw": None})
    # The API needs the transcript to start with a user turn.
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def run_turn(
    session_factory: sessionmaker[Session],
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    chat_model: ChatModel,
    embedder: Embedder,
    max_iterations: int,
    emit: Emit,
    checkpointer: Any = None,
    store: ObjectStore | None = None,
) -> None:
    """Execute one agent run for the latest user message. Never raises: outcomes are recorded
    on the run and the assistant message, and reported through `emit`."""
    with session_factory() as session:
        run = session.get(AgentRun, run_id)
        conversation = session.get(Conversation, run.conversation_id)
        user_msg = session.scalar(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id, ConversationMessage.run_id == run.id,
                   ConversationMessage.role == MessageRole.USER)
        )
        agent = Actor(kind=ActorKind.AGENT, id=run.id, on_behalf_of=run.initiated_by)
        try:
            scope = tenancy.resolve_project_scope(session, agent, tenant_id, project_id)
        except Exception:  # membership revoked between request and run
            finish_run(session, run, None, RunStatus.FAILED, "you no longer have access to this project", emit, [])
            return

        attachment_labels = []
        for vid in user_msg.attachments or []:
            try:
                v = documents.get_version(session, scope, uuid.UUID(vid))
                d = documents.get_document(session, scope, v.document_id)
                attachment_labels.append(f"{d.title} v{v.version_no} (version_id {v.id})")
            except Exception:
                continue

        transcript = _history(session, conversation.id, user_msg.ordinal)
        transcript.append({
            "role": "user",
            "text": f"<project_context>\n{_context_block(session, scope, conversation, attachment_labels)}\n"
                    f"</project_context>\n\n{user_msg.text}",
        })

        ctx = ToolContext(session=session, scope=scope, embedder=embedder, store=store,
                          reason=f"Business Requirements agent run {run.id}")
        loop = Loop(session=session, run=run, chat_model=chat_model, system_prompt=SYSTEM_PROMPT,
                    tools=TOOL_SPECS, handlers=HANDLERS, write_tools=WRITE_TOOLS, ctx=ctx, emit=emit,
                    max_iterations=max_iterations)
        graph = StateGraph(State)
        loop.add_to(graph)
        graph.add_edge(START, "model")
        app = graph.compile(checkpointer=checkpointer)

        try:
            final = app.invoke(
                {"transcript": transcript, "iterations": 0, "stop": None},
                {"configurable": {"thread_id": str(run.id)}, "recursion_limit": 4 * max_iterations + 10},
            )
        except Exception as exc:
            session.rollback()
            record_usage(run, loop.usage, ctx.changeset_seqs)
            if isinstance(exc, LLMError):
                detail = str(exc)
            else:  # unexpected failure: keep what was committed, report it
                traceback.print_exc()
                detail = f"the agent failed unexpectedly ({type(exc).__name__})"
            finish_run(session, run, conversation, RunStatus.FAILED, detail, emit, loop.activity)
            return

        record_usage(run, loop.usage, ctx.changeset_seqs)
        text = last_text(final["transcript"])
        if stop := final.get("stop"):
            detail, text = stop_outcome(stop, text)
            finish_run(session, run, conversation, RunStatus.STOPPED, detail, emit, loop.activity, text=text)
        else:
            finish_run(session, run, conversation, RunStatus.COMPLETED, None, emit, loop.activity, text=text)
