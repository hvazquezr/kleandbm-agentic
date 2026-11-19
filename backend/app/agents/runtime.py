"""The ReAct loop shared by the agents, as LangGraph nodes, plus run bookkeeping.

    model --(tool calls)--> tools --> model ... --(no tool calls / stop)--> <after_loop>

- The execution budget (model calls per run) is enforced here, not by the model.
- Each tool call runs in its own savepoint; write tools commit right away so changes appear
  while the agent works, and a failed tool rolls back cleanly. Tool errors go back to the
  model as is_error results instead of failing the run.
- Events (text deltas, tool activity) go to `emit` for streaming to the browser.
- Everything the model sees and says is written to `run_messages` as it happens and committed,
  so the transcript survives a failed run (`app/agents/transcripts.py`). Entries a node outside
  the loop appends (the Architect's correction turns) are picked up before the next model call.
"""

import json
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.llm import ChatModel, ToolSpec, Usage
from app.adapters.tracing import span, traced_complete
from app.agents.transcripts import RunTranscript
from app.persistence.conversations import AgentRun, Conversation, ConversationMessage, MessageRole, RunStatus

Emit = Callable[[dict[str, Any]], None]


class ToolInputError(Exception):
    """The model called a tool with unusable input. The message goes back to the model."""


class State(TypedDict):
    transcript: list[dict[str, Any]]
    iterations: int
    stop: str | None


@dataclass
class Loop:
    """Everything the model and tools nodes need for one run."""

    session: Session
    run: AgentRun
    chat_model: ChatModel
    system_prompt: str
    tools: list[ToolSpec]
    handlers: dict[str, Callable[[Any, dict], dict]]
    write_tools: set[str]
    ctx: Any
    emit: Emit
    max_iterations: int
    # Tools whose success ends the loop (e.g. a subgraph's structured answer to its caller).
    finish_tools: set[str] = field(default_factory=set)
    # Tools that manage their own transaction (they run a subgraph that commits as it goes), so
    # they run outside the per-call savepoint; the session is clean before each tool call.
    own_transaction: set[str] = field(default_factory=set)
    # How many leading transcript entries are earlier chat turns replayed to the model; they're
    # recorded once, as the "history" segment.
    history: int = 0
    usage: Usage = field(default_factory=Usage)
    activity: list[dict[str, Any]] = field(default_factory=list)
    _streamed: bool = False
    _recorded: int = -1  # transcript entries written so far; -1 before the system prompt

    def _record(self, transcript: list[dict[str, Any]]) -> None:
        """Write the entries not recorded yet, and commit so they outlive a failed run."""
        recorder = RunTranscript(self.session, self.run)
        if self._recorded < 0:
            recorder.system(self.system_prompt)
            if self.history:
                recorder.add(transcript[: self.history], segment="history")
            self._recorded = self.history
        if len(transcript) < self._recorded:  # replaced rather than extended: record it all
            self._recorded = 0
        if len(transcript) > self._recorded:
            recorder.add(transcript[self._recorded:])
            self._recorded = len(transcript)
        self.session.commit()

    def _on_text(self, chunk: str) -> None:
        self.emit({"type": "text", "text": chunk})
        self._streamed = True

    def model_node(self, state: State) -> dict:
        if state["iterations"] >= self.max_iterations:
            return {"stop": "budget"}
        if self._streamed:
            self.emit({"type": "text", "text": "\n\n"})  # keep each step's text in its own paragraph
            self._streamed = False
        self._record(state["transcript"])
        turn = traced_complete(self.chat_model, self.system_prompt, state["transcript"], self.tools,
                               on_text=self._on_text, name=f"{self.run.agent} model")
        RunTranscript(self.session, self.run).add([turn.to_transcript()], stop_reason=turn.stop_reason, model=turn.model)
        self._recorded += 1
        self.session.commit()
        self.usage.add(turn.usage)
        self.run.iterations = state["iterations"] + 1
        if turn.tool_calls and (turn.text or "").strip():
            # Text between steps ("I've read the passages; next I'll…") streams to the user live;
            # keep it in order with the tool activity so a reloaded reply reads the same. Only the
            # final step's text becomes the reply itself.
            self.activity.append({"kind": "narration", "text": turn.text.strip()[:4000]})
        stop = None
        if turn.stop_reason == "refusal":
            stop = "refusal"
        elif turn.stop_reason == "max_tokens" and turn.tool_calls:
            stop = "truncated"  # a cut-off tool input must not run
        return {"transcript": [*state["transcript"], turn.to_transcript()],
                "iterations": state["iterations"] + 1, "stop": stop}

    def tools_node(self, state: State) -> dict:
        last = state["transcript"][-1]
        results = []
        finished = False
        for call in last["tool_calls"]:
            name, args = call["name"], call["input"]
            self.emit({"type": "tool", "status": "started", "name": name})
            handler = self.handlers.get(name)
            own = name in self.own_transaction
            savepoint = None if own else self.session.begin_nested()
            try:
                if handler is None:
                    raise ToolInputError(f"unknown tool {name}")
                with span(name, "tool", {"tool": name, "arguments": args}) as traced:
                    out = handler(self.ctx, args)
                    if traced is not None:
                        traced.end(outputs={"tool": name, "ok": True, "result": out})
                if savepoint is not None:
                    savepoint.commit()
                if name in self.write_tools or own:
                    self.session.commit()  # make the change visible to the user right away
                summary = out.pop("summary", name)
                image = out.pop("__image__", None)
                result = {"id": call["id"], "content": json.dumps(out, default=str), "is_error": False}
                if image:
                    result["image"] = image  # shown to the model, never echoed as text
                results.append(result)
                item = {"tool": name, "ok": True, "summary": summary}
                finished = finished or name in self.finish_tools
                if "code" in out and "id" in out and "type" in out:
                    item["record"] = {"id": out["id"], "code": out["code"], "type": out["type"]}
                    if out.get("action"):  # what happened to it: added, answered, deferred, updated…
                        item["record"]["action"] = out["action"]
            except Exception as exc:
                if savepoint is not None:
                    savepoint.rollback()
                else:
                    self.session.rollback()
                message = str(exc) if isinstance(exc, ToolInputError) else f"tool failed: {type(exc).__name__}"
                if not isinstance(exc, ToolInputError):
                    traceback.print_exc()
                results.append({"id": call["id"], "content": message, "is_error": True})
                item = {"tool": name, "ok": False, "summary": message}
            self.run.tool_calls += 1
            self.activity.append(item)
            self.emit({"type": "tool", "status": "ok" if item["ok"] else "error", "name": name,
                       "summary": item["summary"], "record": item.get("record")})
        update: dict[str, Any] = {"transcript": [*state["transcript"], {"role": "tool", "results": results}]}
        self._record(update["transcript"])
        if finished:
            update["stop"] = "finished"
        return update

    def add_to(self, graph: StateGraph, after_loop: str = END) -> None:
        """Add the "model" and "tools" nodes; the loop exits to `after_loop`."""
        def route(state: State) -> str:
            if state["stop"]:
                return after_loop
            return "tools" if state["transcript"][-1].get("tool_calls") else after_loop

        graph.add_node("model", self.model_node)
        graph.add_node("tools", self.tools_node)
        graph.add_conditional_edges("model", route, ["tools", after_loop])
        # A finish tool ends the loop without another model call.
        graph.add_conditional_edges("tools", lambda s: after_loop if s["stop"] else "model", ["model", after_loop])


def run_config(run: AgentRun, recursion_limit: int) -> dict[str, Any]:
    """LangGraph config for a top-level run. The checkpoint thread is the run id, which the
    server creates inside the caller's scope; nothing reads a checkpoint by a client-supplied
    id. LangGraph's tables have no scope columns, so the scope and actor go in each
    checkpoint's metadata, for audits and for deleting a project's checkpoints."""
    return {"configurable": {"thread_id": str(run.id)}, "recursion_limit": recursion_limit,
            "metadata": {"tenant_id": str(run.tenant_id), "project_id": str(run.project_id),
                         "conversation_id": str(run.conversation_id), "agent": run.agent,
                         "initiated_by": str(run.initiated_by)}}


def turn_message(session: Session, run: AgentRun) -> ConversationMessage | None:
    """The user message a run answers. In an Assistant conversation the message belongs to the
    router run, and the agent answering it is that run's child."""
    owners = [run.id] + ([run.parent_run_id] if run.parent_run_id else [])
    return session.scalar(select(ConversationMessage).where(
        ConversationMessage.conversation_id == run.conversation_id, ConversationMessage.run_id.in_(owners),
        ConversationMessage.role == MessageRole.USER).order_by(ConversationMessage.ordinal.desc()).limit(1))


HISTORY_MESSAGES = 20
AGENT_LABELS = {"business_requirements": "Business Requirements agent", "architect": "Architect"}
CHANGES_MARKER = "**What this run changed:**"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def reply_summary(message: ConversationMessage, agent: str) -> str:
    """Another agent's reply, shortened for this agent's history: who wrote it, the records its
    tools changed, its opening, and the Architect's deterministic change note. Never the whole
    text, so neither agent's context fills with the other's work."""
    by_action: dict[str, list[str]] = {}
    for item in message.activity or []:
        record = item.get("record") if item.get("ok") else None
        if record and record.get("code"):
            codes = by_action.setdefault(record.get("action") or "changed", [])
            if record["code"] not in codes:
                codes.append(record["code"])
    text = message.text or ""
    head, marker, changes = text.partition(CHANGES_MARKER)
    parts = [f"[The {AGENT_LABELS.get(agent, agent)} answered this message. Summary written by the "
             f"application; the full reply isn't shown.]",
             f"Its reply began: \"{_clip(head, 300)}\""]
    if by_action:
        parts.append("Register entries its tools changed: "
                     + "; ".join(f"{action} {', '.join(codes)}" for action, codes in by_action.items()) + ".")
    if marker:
        parts.append(marker + " " + _clip(changes, 900))
    return "\n".join(parts)


def conversation_history(session: Session, conversation_id, upto_ordinal: int, agent: str) -> list[dict[str, Any]]:
    """Earlier turns of the conversation for `agent`. Its own replies and the router's (short
    questions and notes) are in full; replies by the other agent, in an Assistant conversation,
    are summaries (`reply_summary`). Consecutive replies in one turn are joined."""
    rows = list(session.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id, ConversationMessage.ordinal < upto_ordinal)
        .order_by(ConversationMessage.ordinal.desc())
        .limit(HISTORY_MESSAGES)
    ))
    rows.reverse()
    run_ids = [m.run_id for m in rows if m.role is MessageRole.ASSISTANT and m.run_id]
    authors = dict(session.execute(select(AgentRun.id, AgentRun.agent).where(AgentRun.id.in_(run_ids))).all()) \
        if run_ids else {}
    out: list[dict[str, Any]] = []
    for m in rows:
        if m.role is MessageRole.USER:
            out.append({"role": "user", "text": m.text})
            continue
        author = authors.get(m.run_id, agent)
        text = reply_summary(m, author) if author in AGENT_LABELS and author != agent else m.text
        if out and out[-1]["role"] == "assistant":
            out[-1]["text"] += "\n\n" + text
        else:
            out.append({"role": "assistant", "text": text, "tool_calls": [], "raw": None})
    # The API needs the transcript to start with a user turn.
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def last_text(transcript: list[dict[str, Any]]) -> str:
    return next((t["text"] for t in reversed(transcript) if t["role"] == "assistant" and t.get("text")), "")


STOP_NOTES = {
    "budget": ("execution budget reached",
               "I reached my step limit for this message. What I recorded so far is saved; send a message to continue."),
    "refusal": ("the model declined to continue", "I can't help with that request."),
    "truncated": ("a tool call was cut off by the output limit",
                  "My response was cut off. Please send the message again."),
}


def stop_outcome(stop: str, text: str) -> tuple[str, str]:
    """(status detail, reply text) for a run that stopped early."""
    detail, note = STOP_NOTES[stop]
    if stop == "budget":
        return detail, (text + "\n\n" + note).strip()
    return detail, text or note


def record_usage(run: AgentRun, usage: Usage, changeset_seqs: list[int]) -> None:
    run.input_tokens, run.output_tokens = usage.input_tokens, usage.output_tokens
    run.cache_read_tokens, run.cache_write_tokens = usage.cache_read_tokens, usage.cache_write_tokens
    run.changeset_seqs = list(changeset_seqs)


def finish_run(
    session: Session,
    run: AgentRun,
    conversation: Conversation | None,
    status: RunStatus,
    detail: str | None,
    emit: Emit,
    activity: list[dict[str, Any]],
    text: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record the outcome and the assistant's reply, then send the final "done" event."""
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
        **(extra or {}),
    })
