"""The Business Requirements agent as an explicit LangGraph workflow.

    START -> model --(tool calls)--> tools --> model ... --> END

- One graph run answers one user message (one AgentRun). The graph state holds a neutral
  transcript; LangGraph checkpoints it per run (thread_id = run id), separate from the
  registers.
- The execution budget (model calls per run) is enforced by the graph, not by the model.
- Each tool call runs in its own savepoint and is committed on success, so register changes
  appear while the agent is still working, and a failed tool rolls back cleanly.
- Events (text deltas, tool activity) go to `emit` for streaming to the browser.
"""

import json
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.embeddings import Embedder
from app.adapters.llm import ChatModel, LLMError, Usage
from app.agents.requirements.prompt import SYSTEM_PROMPT
from app.agents.requirements.tools import HANDLERS, TOOL_SPECS, WRITE_TOOLS, ToolContext, ToolInputError
from app.domain import documents, modeling, tenancy
from app.domain.access import Actor, ActorKind
from app.persistence.conversations import AgentRun, Conversation, ConversationMessage, MessageRole, RunStatus
from app.persistence.modeling import DataModel
from app.persistence.models import Project

AGENT_NAME = "business_requirements"
HISTORY_MESSAGES = 20
Emit = Callable[[dict[str, Any]], None]


class State(TypedDict):
    transcript: list[dict[str, Any]]
    iterations: int
    stop: str | None


def _context_block(session: Session, scope, conversation: Conversation, attachments: list[str]) -> str:
    """Per-run project facts. Volatile, so it goes in the user turn, not the cached system prompt."""
    project = session.get(Project, scope.project_id)
    lines = [f"Project: {project.name}" + (f" — {project.description}" if project.description else "")]
    if conversation.model_id:
        model = session.get(DataModel, conversation.model_id)
        if model:
            lines.append(f"Data model: {model.name}" + (f" — {model.description}" if model.description else ""))
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
            _finish(session, run, None, RunStatus.FAILED, "you no longer have access to this project", emit, [])
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

        ctx = ToolContext(session=session, scope=scope, embedder=embedder,
                          reason=f"Business Requirements agent run {run.id}")
        usage = Usage()
        activity: list[dict[str, Any]] = []
        streamed = {"text": False}  # whether any reply text has been streamed yet this run

        def on_text(chunk: str) -> None:
            emit({"type": "text", "text": chunk})
            streamed["text"] = True

        def model_node(state: State) -> dict:
            if state["iterations"] >= max_iterations:
                return {"stop": "budget"}
            if streamed["text"]:
                emit({"type": "text", "text": "\n\n"})  # keep each step's text in its own paragraph
                streamed["text"] = False
            turn = chat_model.complete(SYSTEM_PROMPT, state["transcript"], TOOL_SPECS, on_text=on_text)
            usage.add(turn.usage)
            run.iterations = state["iterations"] + 1
            stop = None
            if turn.stop_reason == "refusal":
                stop = "refusal"
            elif turn.stop_reason == "max_tokens" and turn.tool_calls:
                stop = "truncated"  # a cut-off tool input must not run
            return {"transcript": [*state["transcript"], turn.to_transcript()],
                    "iterations": state["iterations"] + 1, "stop": stop}

        def tools_node(state: State) -> dict:
            last = state["transcript"][-1]
            results = []
            for call in last["tool_calls"]:
                name, args = call["name"], call["input"]
                emit({"type": "tool", "status": "started", "name": name})
                handler = HANDLERS.get(name)
                savepoint = session.begin_nested()
                try:
                    if handler is None:
                        raise ToolInputError(f"unknown tool {name}")
                    out = handler(ctx, args)
                    savepoint.commit()
                    if name in WRITE_TOOLS:
                        session.commit()  # make the change visible to the user right away
                    summary = out.pop("summary", name)
                    results.append({"id": call["id"], "content": json.dumps(out, default=str), "is_error": False})
                    item = {"tool": name, "ok": True, "summary": summary}
                    if "code" in out:
                        item["record"] = {"id": out["id"], "code": out["code"], "type": out["type"]}
                except (ToolInputError, Exception) as exc:
                    savepoint.rollback()
                    message = str(exc) if isinstance(exc, ToolInputError) else f"tool failed: {type(exc).__name__}"
                    if not isinstance(exc, ToolInputError):
                        traceback.print_exc()
                    results.append({"id": call["id"], "content": message, "is_error": True})
                    item = {"tool": name, "ok": False, "summary": message}
                run.tool_calls += 1
                activity.append(item)
                emit({"type": "tool", "status": "ok" if item["ok"] else "error", "name": name,
                      "summary": item["summary"], "record": item.get("record")})
            return {"transcript": [*state["transcript"], {"role": "tool", "results": results}]}

        def after_model(state: State) -> str:
            if state["stop"]:
                return END
            last = state["transcript"][-1]
            return "tools" if last.get("tool_calls") else END

        graph = StateGraph(State)
        graph.add_node("model", model_node)
        graph.add_node("tools", tools_node)
        graph.add_edge(START, "model")
        graph.add_conditional_edges("model", after_model, ["tools", END])
        graph.add_edge("tools", "model")
        app = graph.compile(checkpointer=checkpointer)

        try:
            final = app.invoke(
                {"transcript": transcript, "iterations": 0, "stop": None},
                {"configurable": {"thread_id": str(run.id)}, "recursion_limit": 4 * max_iterations + 10},
            )
        except LLMError as exc:
            session.rollback()
            _record_usage(run, usage, ctx)
            _finish(session, run, conversation, RunStatus.FAILED, str(exc), emit, activity)
            return
        except Exception as exc:  # unexpected failure: keep what was committed, report it
            traceback.print_exc()
            session.rollback()
            _record_usage(run, usage, ctx)
            _finish(session, run, conversation, RunStatus.FAILED, f"the agent failed unexpectedly ({type(exc).__name__})", emit, activity)
            return

        _record_usage(run, usage, ctx)
        last_text = next((t["text"] for t in reversed(final["transcript"])
                          if t["role"] == "assistant" and t.get("text")), "")
        stop = final.get("stop")
        if stop == "budget":
            note = "I reached my step limit for this message. What I recorded so far is saved; send a message to continue."
            _finish(session, run, conversation, RunStatus.STOPPED, "execution budget reached", emit, activity,
                    text=(last_text + "\n\n" + note).strip())
        elif stop == "refusal":
            _finish(session, run, conversation, RunStatus.STOPPED, "the model declined to continue", emit, activity,
                    text=last_text or "I can't help with that request.")
        elif stop == "truncated":
            _finish(session, run, conversation, RunStatus.STOPPED, "a tool call was cut off by the output limit",
                    emit, activity, text=last_text or "My response was cut off. Please send the message again.")
        else:
            _finish(session, run, conversation, RunStatus.COMPLETED, None, emit, activity, text=last_text)


def _record_usage(run: AgentRun, usage: Usage, ctx: ToolContext) -> None:
    run.input_tokens, run.output_tokens = usage.input_tokens, usage.output_tokens
    run.cache_read_tokens, run.cache_write_tokens = usage.cache_read_tokens, usage.cache_write_tokens
    run.changeset_seqs = list(ctx.changeset_seqs)


def _finish(
    session: Session,
    run: AgentRun,
    conversation: Conversation | None,
    status: RunStatus,
    detail: str | None,
    emit: Emit,
    activity: list[dict[str, Any]],
    text: str | None = None,
) -> None:
    run.status = status
    run.status_detail = detail
    run.finished_at = datetime.now(UTC)
    message = None
    if conversation is not None:
        if text is None:
            text = f"Sorry, I couldn't finish: {detail}"
        next_ordinal = (session.scalar(
            select(ConversationMessage.ordinal)
            .where(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.ordinal.desc()).limit(1)
        ) or 0) + 1
        message = ConversationMessage(
            tenant_id=run.tenant_id, project_id=run.project_id, conversation_id=conversation.id,
            ordinal=next_ordinal, role=MessageRole.ASSISTANT, text=text, attachments=[],
            activity=activity, run_id=run.id, author_id=run.id,
        )
        session.add(message)
    session.commit()
    emit({
        "type": "done",
        "status": str(status),
        "detail": detail,
        "message_id": str(message.id) if message else None,
        "run": {"id": str(run.id), "iterations": run.iterations, "tool_calls": run.tool_calls,
                "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
                "cache_read_tokens": run.cache_read_tokens},
    })
