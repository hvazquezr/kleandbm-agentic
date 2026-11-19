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

Routes: requirements, architect, requirements_then_architect, clarify. Chaining the Architect
after the Business Requirements run is backlog step 2; until then that route runs the Business
Requirements agent only and says so in the stored decision.
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
from app.persistence.modeling import DataModel

AGENT_NAME = "router"
ROUTES = ("requirements", "architect", "requirements_then_architect", "clarify")
# Which agents answer each route, in order. Step 1 runs the first one only.
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
    """Runs one agent for a run id: `model` is the model name recorded on the run."""

    model: str
    run: Callable[[uuid.UUID, Emit], None]


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
            if len(plan) > 1:
                reason += " (The Architect step isn't chained yet; ask the Architect once the requirement is recorded.)"
            session.add(RoutingDecision(tenant_id=run.tenant_id, project_id=run.project_id, run_id=run.id,
                                        route=decision["route"], reason=reason, source=source,
                                        router_model=router_model))
            session.commit()
            emit({"type": "route", "route": decision["route"], "reason": reason, "source": source,
                  "agents": plan[:1]})

            if not plan:
                finish_run(session, run, conversation, RunStatus.COMPLETED, None, emit, [],
                           text=decision["question"] or FALLBACK_QUESTION)
                return

            agent = plan[0]
            try:
                runner = runners(agent)
            except LLMError as exc:
                finish_run(session, run, conversation, RunStatus.FAILED, str(exc), emit, [],
                           text=f"The {AGENT_LABELS[agent]} isn't available: {exc}")
                return
            child = AgentRun(tenant_id=run.tenant_id, project_id=run.project_id, conversation_id=run.conversation_id,
                             agent=agent, parent_run_id=run.id, llm_model=runner.model, status=RunStatus.RUNNING,
                             initiated_by=run.initiated_by, changeset_seqs=[])
            session.add(child)
            session.commit()
            child_id = child.id
            try:
                runner.run(child_id, emit)  # writes the reply and sends "done"
            finally:
                session.expire_all()
                child = session.get(AgentRun, child_id)
                if child.status is RunStatus.RUNNING:  # its runner couldn't start it
                    _close(session, child, RunStatus.FAILED, "the agent could not start")
                failed = child.status is RunStatus.FAILED
                _close(session, run, RunStatus.FAILED if failed else RunStatus.COMPLETED,
                       child.status_detail if failed else None)
