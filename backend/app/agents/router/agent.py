"""The request router (spec §8, "Proposal under discussion — one conversation with a request
router"; backlog, step 1).

In an Assistant conversation every user message gets a router run. The router makes one model
call with a single tool, `route_request`, and no tool that reads or writes project content: its
input is the user's message, the user's earlier messages with the agent that answered each, and
a project summary the application builds. It never sees document passages, source values or
agent replies, so untrusted content can't steer it.

The decision is stored (`routing_decisions`), and the chosen agent answers as a child run of the
router run, so usage, transcripts, tracing and the monitor follow the usual parent/child path.
A message sent with a known target (an action such as Generate draft, or the user choosing the
other agent) skips the model call and records `source="user"`.

Routes: requirements, architect, requirements_then_architect, clarify. The chained route (backlog
step 2) runs the Business Requirements agent, then the Architect as a second child, once the
first run's register changes are committed. The chain stops when that run didn't complete or
raised a blocking question: the router adds a note saying why, and the Architect doesn't design
on a meaning that isn't settled. The Architect gets an application-built handoff note (the record
codes the first run wrote), never the first agent's reply text. Only the turn's last agent sends
"done"; the first agent's becomes "step_done", followed by a "handoff" event.
"""

import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters import tracing
from app.adapters.llm import ChatModel, LLMError, ToolSpec, Usage
from app.adapters.tracing import traced_complete
from app.agents.router.prompt import SYSTEM_PROMPT
from app.agents.runtime import Emit, finish_run, record_usage
from app.agents.transcripts import RunTranscript
from app.domain import modeling, sources, tenancy
from app.domain.access import Actor, ActorKind
from app.domain.records import ConfirmationStatus, QuestionStatus
from app.persistence.conversations import (
    AgentRun, Conversation, ConversationMessage, MessageRole, RoutingDecision, RunStatus,
)
from app.persistence.design import CandidateStatus, DesignCandidate
from app.persistence.modeling import DataModel, QuestionRevision

AGENT_NAME = "router"
ROUTES = ("requirements", "architect", "requirements_then_architect", "clarify")
# Which agents answer each route, in order.
PLAN = {"requirements": ["business_requirements"], "architect": ["architect"],
        "requirements_then_architect": ["business_requirements", "architect"], "clarify": []}
TARGETS = {"business_requirements": "requirements", "architect": "architect"}
AGENT_LABELS = {"business_requirements": "Requirements", "architect": "Architect"}
HISTORY_TURNS = 6
FALLBACK_QUESTION = ("Should I record this as a business requirement or definition, or change the "
                     "data model? Reply with which you mean, or pick an agent when you send it.")

ROUTE_SPEC = ToolSpec(
    name="route_request",
    description="Send the user's message to the agent that should answer it, or ask the user to clarify.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["route", "reason", "question"],
        "properties": {
            "route": {"type": "string", "enum": list(ROUTES)},
            "reason": {"type": "string", "description": "One plain sentence for the user: what the message asks for."},
            "question": {"type": ["string", "null"],
                         "description": "For clarify only: one short question that lets the user choose. Null otherwise."},
        },
    },
)


@dataclass
class Runner:
    """Runs one agent for a run id: `model` is the model name recorded on the run. The third
    argument is a handoff note from an earlier agent in the same turn, or None."""

    model: str
    run: Callable[[uuid.UUID, Emit, str | None], None]


# Builds the runner for an agent; raises LLMError when its model isn't configured.
RunnerFactory = Callable[[str], Runner]


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def project_state(session: Session, scope, model_id: uuid.UUID) -> str:
    """Facts the application knows, never agent-written or untrusted text (names excepted)."""
    model = session.get(DataModel, model_id)
    seq = modeling.current_seq(session, scope)
    reqs = modeling._latest(session, scope, modeling.RecordType.REQUIREMENT, as_of=seq)
    defs = modeling._latest(session, scope, modeling.RecordType.DEFINITION, as_of=seq)
    questions = [q for q in modeling._latest(session, scope, modeling.RecordType.QUESTION, as_of=seq)
                 if q.data.get("status") == QuestionStatus.OPEN.value]
    unconfirmed = sum(1 for d in defs if d.data.get("confirmation_status") == ConfirmationStatus.UNCONFIRMED.value)
    tables = modeling._latest(session, scope, modeling.RecordType.TABLE, as_of=seq, model_id=model_id, model_filter=True)
    proposed = session.scalar(select(DesignCandidate.id).where(
        DesignCandidate.tenant_id == scope.tenant_id, DesignCandidate.project_id == scope.project_id,
        DesignCandidate.model_id == model_id, DesignCandidate.status == CandidateStatus.PROPOSED).limit(1))
    linked = [c for c in sources.model_sources(session, scope, model_id) if not c.disabled_at]
    return "\n".join([
        f"Data model: {_clip(model.name, 120) if model else 'unknown'}",
        f"Model tables: {len(tables)}" + (" (empty model)" if not tables else ""),
        f"Requirements: {len(reqs)}",
        f"Business definitions: {len(defs)} ({unconfirmed} unconfirmed)",
        f"Open questions: {len(questions)} ({sum(1 for q in questions if q.data.get('is_blocking'))} blocking)",
        f"Design proposal awaiting review: {'yes' if proposed else 'no'}",
        f"Linked data sources: {len(linked)}",
    ])


def _earlier_turns(session: Session, conversation_id: uuid.UUID, before: int) -> list[str]:
    """The user's earlier messages and which agent answered each. Agent replies are left out:
    they can quote documents and source values, which the router must not read."""
    rows = list(session.scalars(
        select(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id,
                                          ConversationMessage.ordinal < before)
        .order_by(ConversationMessage.ordinal.desc()).limit(4 * HISTORY_TURNS)))
    agents = {r.id: r.agent for r in session.scalars(select(AgentRun).where(
        AgentRun.id.in_([m.run_id for m in rows if m.role is MessageRole.ASSISTANT and m.run_id])))}
    lines: list[str] = []
    answered_by: str | None = None
    for m in rows:  # newest first
        if m.role is MessageRole.ASSISTANT:
            answered_by = answered_by or AGENT_LABELS.get(agents.get(m.run_id), "the assistant (a question back)")
            continue
        lines.append(f"- User: {_clip(m.text, 400)} → answered by {answered_by or 'nobody yet'}")
        answered_by = None
        if len(lines) == HISTORY_TURNS:
            break
    return list(reversed(lines))


def brief(state: str, earlier: list[str], message: str) -> str:
    parts = [f"<project_state>\n{state}\n</project_state>"]
    if earlier:
        parts.append("<earlier_messages>\n" + "\n".join(earlier) + "\n</earlier_messages>")
    parts.append(f"<message>\n{message}\n</message>")
    return "\n\n".join(parts)


def decide(chat_model: ChatModel, run: AgentRun, session: Session, user_brief: str) -> tuple[dict[str, Any], Usage]:
    """One model call. Returns the validated tool input (or a clarify fallback) and usage."""
    transcript = [{"role": "user", "text": user_brief}]
    recorder = RunTranscript(session, run)
    recorder.system(SYSTEM_PROMPT)
    recorder.add(transcript)
    session.commit()
    turn = traced_complete(chat_model, SYSTEM_PROMPT, transcript, [ROUTE_SPEC], name="router model")
    RunTranscript(session, run).add([turn.to_transcript()], stop_reason=turn.stop_reason, model=turn.model)
    run.iterations = 1
    session.commit()
    call = next((c for c in turn.tool_calls if c.name == ROUTE_SPEC.name), None)
    args = call.input if call else {}
    route = args.get("route")
    reason = (args.get("reason") or "").strip()
    if route not in ROUTES or not reason:
        return {"route": "clarify", "reason": "The router couldn't tell which agent this is for.",
                "question": FALLBACK_QUESTION}, turn.usage
    question = (args.get("question") or "").strip() or (FALLBACK_QUESTION if route == "clarify" else None)
    return {"route": route, "reason": _clip(reason, 400), "question": question}, turn.usage


def _close(session: Session, run: AgentRun, status: RunStatus, detail: str | None) -> None:
    run.status, run.status_detail, run.finished_at = status, detail, datetime.now(UTC)
    session.commit()


def route_turn(
    session_factory: sessionmaker[Session],
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    chat_model: ChatModel | None,
    target: str | None,
    runners: RunnerFactory,
    emit: Emit,
) -> None:
    """Route one user message and run the agent that answers it. `target` (an agent name) skips
    the model call. Never raises: outcomes are recorded on the runs and the conversation."""
    with session_factory() as session:
        run = session.get(AgentRun, run_id)
        conversation = session.get(Conversation, run.conversation_id)
        user_msg = session.scalar(select(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation.id, ConversationMessage.run_id == run.id,
            ConversationMessage.role == MessageRole.USER))
        actor = Actor(kind=ActorKind.AGENT, id=run.id, on_behalf_of=run.initiated_by)
        try:
            scope = tenancy.resolve_project_scope(session, actor, tenant_id, project_id)
            modeling.get_model(session, scope, conversation.model_id)
        except Exception:  # membership revoked, or the model was deleted
            finish_run(session, run, None, RunStatus.FAILED, "you no longer have access to this model", emit, [])
            return

        with tracing.agent_run(run):
            if target:
                decision = {"route": TARGETS[target], "reason": f"You sent this to the {AGENT_LABELS[target]}.",
                            "question": None}
                source, router_model = "user", None
            else:
                try:
                    state = project_state(session, scope, conversation.model_id)
                    earlier = _earlier_turns(session, conversation.id, user_msg.ordinal)
                    decision, usage = decide(chat_model, run, session, brief(state, earlier, user_msg.text))
                    record_usage(run, usage, [])
                except Exception as exc:
                    session.rollback()
                    if not isinstance(exc, LLMError):
                        traceback.print_exc()
                    detail = str(exc) if isinstance(exc, LLMError) else f"the router failed ({type(exc).__name__})"
                    finish_run(session, run, conversation, RunStatus.FAILED, detail, emit, [],
                               text="I couldn't route your message. Send it again, or pick the Requirements "
                                    "agent or the Architect for it.")
                    return
                source, router_model = "router", chat_model.model

            plan = PLAN[decision["route"]]
            reason = decision["reason"]
            session.add(RoutingDecision(tenant_id=run.tenant_id, project_id=run.project_id, run_id=run.id,
                                        route=decision["route"], reason=reason, source=source,
                                        router_model=router_model))
            session.commit()
            emit({"type": "route", "route": decision["route"], "reason": reason, "source": source,
                  "agents": plan})

            if not plan:
                finish_run(session, run, conversation, RunStatus.COMPLETED, None, emit, [],
                           text=decision["question"] or FALLBACK_QUESTION)
                return

            try:
                runner = runners(plan[0])
            except LLMError as exc:
                finish_run(session, run, conversation, RunStatus.FAILED, str(exc), emit, [],
                           text=f"The {AGENT_LABELS[plan[0]]} isn't available: {exc}")
                return

            failed_detail = None
            note = None
            try:
                for step, agent in enumerate(plan):
                    last = step == len(plan) - 1
                    held: list[dict[str, Any]] = []

                    def step_emit(event: dict[str, Any], *, _held=held, _last=last) -> None:
                        # Only the turn's last agent sends "done"; an earlier one's is held until
                        # the router knows whether the chain goes on.
                        if event.get("type") == "done" and not _last:
                            _held.append(event)
                        else:
                            emit(event)

                    child = AgentRun(tenant_id=run.tenant_id, project_id=run.project_id,
                                     conversation_id=run.conversation_id, agent=agent, parent_run_id=run.id,
                                     llm_model=runner.model, status=RunStatus.RUNNING,
                                     initiated_by=run.initiated_by, changeset_seqs=[])
                    session.add(child)
                    session.commit()
                    child_id = child.id
                    try:
                        runner.run(child_id, step_emit, note)  # writes the reply and sends "done"
                    finally:
                        session.expire_all()
                        child = session.get(AgentRun, child_id)
                        if child.status is RunStatus.RUNNING:  # its runner couldn't start it
                            _close(session, child, RunStatus.FAILED, "the agent could not start")
                    if child.status is RunStatus.FAILED:
                        failed_detail = child.status_detail or "the agent failed"
                    if last:
                        break
                    nxt = plan[step + 1]
                    stop = _chain_stop(session, scope, child)
                    if not stop:
                        try:
                            runner = runners(nxt)
                        except LLMError as exc:
                            stop = (f"The {AGENT_LABELS[nxt]} isn't available ({exc}), so it didn't work "
                                    f"on the design part of this message.")
                    if stop:
                        _stop_chain(session, run, conversation, emit, held, stop)
                        break
                    for event in held:  # the first agent's outcome, not the turn's end
                        emit({**event, "type": "step_done", "agent": agent})
                    note = handoff_note(session, child)
                    emit({"type": "handoff", "from": agent, "to": nxt, "note": note})
            finally:
                session.expire_all()
                router_run = session.get(AgentRun, run_id)
                if router_run.status is RunStatus.RUNNING:
                    _close(session, router_run, RunStatus.FAILED if failed_detail else RunStatus.COMPLETED,
                           failed_detail)


def _new_blocking_questions(session: Session, scope, run: AgentRun) -> list[modeling.RecordView]:
    """Open blocking questions that the run created or changed."""
    seqs = list(run.changeset_seqs or [])
    if not seqs:
        return []
    ids = list(set(session.scalars(select(QuestionRevision.record_id).where(
        QuestionRevision.tenant_id == scope.tenant_id, QuestionRevision.project_id == scope.project_id,
        QuestionRevision.seq.in_(seqs)))))
    if not ids:
        return []
    current = modeling._latest(session, scope, modeling.RecordType.QUESTION,
                               as_of=modeling.current_seq(session, scope), record_ids=ids)
    return [q for q in current if q.data.get("status") == QuestionStatus.OPEN.value and q.data.get("is_blocking")]


def _chain_stop(session: Session, scope, child: AgentRun) -> str | None:
    """Why the Architect shouldn't design on top of the Business Requirements run, or None."""
    label = AGENT_LABELS.get(child.agent, child.agent)
    if child.status is not RunStatus.COMPLETED:
        return (f"I didn't start the Architect, because the {label} agent didn't finish "
                f"({child.status_detail or child.status}). Send the message again when you're ready.")
    blocking = _new_blocking_questions(session, scope, child)
    if blocking:
        codes = ", ".join(q.code or "a question" for q in blocking)
        return (f"I didn't start the Architect, because the {label} agent raised a blocking question "
                f"({codes}). Answer it first, then ask for the design change; the Architect would "
                f"otherwise design on a meaning that isn't settled.")
    return None


def handoff_note(session: Session, child: AgentRun) -> str:
    """What the earlier agent did, built from its tool activity (record codes and actions), never
    from its reply text."""
    reply = session.scalar(select(ConversationMessage).where(
        ConversationMessage.run_id == child.id, ConversationMessage.role == MessageRole.ASSISTANT).limit(1))
    by_action: dict[str, list[str]] = {}
    for item in (reply.activity if reply else []) or []:
        record = item.get("record") if item.get("ok") else None
        if record and record.get("code"):
            codes = by_action.setdefault(record.get("action") or "changed", [])
            if record["code"] not in codes:
                codes.append(record["code"])
    label = AGENT_LABELS.get(child.agent, child.agent)
    done = "; ".join(f"{action} {', '.join(codes)}" for action, codes in by_action.items())
    return (f"The {label} agent handled this message first and its register changes are committed"
            + (f" ({done})" if done else " (it changed no register entries)")
            + ". Design from the registers as they are now; the design part of the message is yours.")


def _stop_chain(session: Session, run: AgentRun, conversation: Conversation, emit: Emit,
                held: list[dict[str, Any]], text: str) -> None:
    """End the turn after the first agent: the router's note is the last reply, and the first
    agent's held "done" ends the stream with the chain marked stopped."""
    next_ordinal = (session.scalar(select(ConversationMessage.ordinal).where(
        ConversationMessage.conversation_id == conversation.id)
        .order_by(ConversationMessage.ordinal.desc()).limit(1)) or 0) + 1
    note = ConversationMessage(tenant_id=run.tenant_id, project_id=run.project_id,
                               conversation_id=conversation.id, ordinal=next_ordinal, role=MessageRole.ASSISTANT,
                               text=text, attachments=[], activity=[], run_id=run.id, author_id=run.id)
    session.add(note)
    session.commit()
    for event in held or [{"type": "done", "status": str(RunStatus.COMPLETED), "detail": None}]:
        emit({**event, "chain_stopped": text, "note_message_id": str(note.id)})
