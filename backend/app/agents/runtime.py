"""The ReAct loop shared by the agents, as LangGraph nodes, plus run bookkeeping.

    model --(tool calls)--> tools --> model ... --(no tool calls / stop)--> <after_loop>

- The execution budget (model calls per run) is enforced here, not by the model.
- Each tool call runs in its own savepoint; write tools commit right away so changes appear
  while the agent works, and a failed tool rolls back cleanly. Tool errors go back to the
  model as is_error results instead of failing the run.
- Events (text deltas, tool activity) go to `emit` for streaming to the browser.
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
    usage: Usage = field(default_factory=Usage)
    activity: list[dict[str, Any]] = field(default_factory=list)
    _streamed: bool = False

    def _on_text(self, chunk: str) -> None:
        self.emit({"type": "text", "text": chunk})
        self._streamed = True

    def model_node(self, state: State) -> dict:
        if state["iterations"] >= self.max_iterations:
            return {"stop": "budget"}
        if self._streamed:
            self.emit({"type": "text", "text": "\n\n"})  # keep each step's text in its own paragraph
            self._streamed = False
        turn = self.chat_model.complete(self.system_prompt, state["transcript"], self.tools, on_text=self._on_text)
        self.usage.add(turn.usage)
        self.run.iterations = state["iterations"] + 1
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
                out = handler(self.ctx, args)
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
