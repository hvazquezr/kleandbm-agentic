"""Conversations with agents. Sending a message streams the agent's reply as Server-Sent
Events:

  data: {"type": "run", "run_id": ...}
  data: {"type": "text", "text": "..."}                       (incremental reply text)
  data: {"type": "tool", "status": "started|ok|error", "name": ..., "summary": ..., "record": ...}
  data: {"type": "done", "status": ..., "message_id": ..., "run": {usage...}}

In an Assistant conversation every event after "run" also carries `step` (the agent answering
that part of the turn, or "router") and `agent` (who produced it; inside an Architect step that
can be a consultation or a Validator round). The router adds:

  data: {"type": "route", "route": ..., "reason": ..., "source": "router|user", "agents": [...]}
  data: {"type": "step_done", ...done fields}                 (a chained turn's first agent)
  data: {"type": "handoff", "from": ..., "to": ..., "note": ...}

and a stopped chain's "done" has `chain_stopped` (the router's note) and `note_message_id`.
"""

import json
import queue
import threading
import traceback
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.adapters.llm import LLMError
from app.agents.architect import search
from app.agents.architect.agent import run_design
from app.agents.monitor.monitor import monitor_run
from app.agents.requirements.agent import run_turn
from app.agents.router.agent import Runner, route_turn
from app.api.source_routes import get_box
from app.api.deps import ProjectScopeDep, SessionDep
from app.config import get_settings
from app.persistence.conversations import AgentRun, RunStatus
from app.domain import conversations, flags

def _mark_failed(session_factory, run_id: uuid.UUID, detail: str) -> None:
    """Never leave a run 'running': that would block the conversation for good."""
    from datetime import UTC, datetime

    with session_factory() as session:
        run = session.get(AgentRun, run_id)
        if run is not None and run.status is RunStatus.RUNNING:
            run.status, run.status_detail, run.finished_at = RunStatus.FAILED, detail, datetime.now(UTC)
            session.commit()


def _checkpointer(app_state):
    holder = app_state.agent_checkpointer
    # A lazy holder opens Postgres on first use; anything else is a ready checkpointer (or None).
    return holder.resolve() if hasattr(holder, "resolve") else holder


router = APIRouter(prefix="/tenants/{tenant_id}/projects/{project_id}", tags=["conversations"])


class ConversationIn(BaseModel):
    # An Assistant conversation belongs to a model; single-agent conversations are refused (409).
    model_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=255)
    agent: str = "assistant"


class ConversationOut(BaseModel):
    id: uuid.UUID
    model_id: uuid.UUID | None
    agent: str
    title: str
    created_at: datetime


class MessageIn(BaseModel):
    text: str = Field(default="", max_length=20_000)
    attachment_version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    # Assistant conversations only: send the message straight to this agent instead of letting
    # the request router choose (an action with a known target, or "send to the other agent").
    agent: Literal["business_requirements", "architect"] | None = None
    # With `agent`: the earlier turn (its router run) whose route this message overrides, when
    # the user sends the same message to the other agent instead. Recorded on the decision.
    instead_of_run_id: uuid.UUID | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    ordinal: int
    role: str
    text: str
    attachments: list[str]
    activity: list[dict[str, Any]]
    run_id: uuid.UUID | None
    # The agent that wrote an assistant message (business_requirements, architect, or router
    # for a question back); None for the user's messages.
    agent: str | None = None
    # The top-level run of the turn: the router run in an Assistant conversation, else run_id.
    # Monitor assessments are keyed by it.
    turn_run_id: uuid.UUID | None = None
    # On a user message in an Assistant conversation: where the router (or the user) sent it.
    route: "RouteOut | None" = None
    created_at: datetime


class RouteOut(BaseModel):
    route: str  # requirements | architect | requirements_then_architect | clarify
    reason: str
    source: str  # router | user
    router_model: str | None
    # The earlier turn this one overrode ("send to the other agent instead").
    overrides_run_id: uuid.UUID | None = None
    created_at: datetime


MessageOut.model_rebuild()


def _route_out(d) -> RouteOut | None:
    return RouteOut(route=d.route, reason=d.reason, source=d.source, router_model=d.router_model,
                    overrides_run_id=d.overrides_run_id, created_at=d.created_at) if d else None


class RunOut(BaseModel):
    id: uuid.UUID
    agent: str
    llm_model: str
    # A consultation run (e.g. the Business Requirements agent answering the Architect) names
    # the run that started it; a run lists the consultations it started.
    parent_run_id: uuid.UUID | None
    child_run_ids: list[uuid.UUID]
    status: str
    status_detail: str | None
    iterations: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    changeset_seqs: list[int]
    started_at: datetime
    finished_at: datetime | None
    # For a router run: where it sent the message and why.
    route: RouteOut | None = None


class AssessmentRefOut(BaseModel):
    agent: str
    ordinal: int


class ConcernOut(BaseModel):
    kind: str
    explanation: str
    entries: list[AssessmentRefOut]


class UntrustedInstructionOut(BaseModel):
    agent: str
    ordinal: int
    quote: str
    followed: bool


class AssessmentReviewOut(BaseModel):
    id: uuid.UUID
    verdict: str  # concern_confirmed | false_alarm
    note: str | None
    by_name: str | None
    created_at: datetime


class AssessmentReviewIn(BaseModel):
    verdict: Literal["concern_confirmed", "false_alarm"]
    note: str | None = Field(default=None, max_length=2000)


class AssessmentOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    status: str
    monitor_model: str | None
    score: int | None
    flagged: bool
    summary: str | None
    concerns: list[ConcernOut]
    untrusted_instructions: list[UntrustedInstructionOut]
    samples: list[int]
    input_tokens: int
    output_tokens: int
    truncated: bool
    detail: str | None
    created_at: datetime
    finished_at: datetime | None
    # People's verdicts on this assessment, newest first (the first is the current one).
    reviews: list[AssessmentReviewOut] = []


class ContentFlagOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    agent: str
    target_kind: str  # document | source
    document_id: uuid.UUID | None
    document_version_id: uuid.UUID | None
    document_title: str | None
    version_no: int | None
    location: dict[str, Any] | None
    source_id: uuid.UUID | None
    source_name: str | None
    source_table: str | None
    source_column: str | None
    quote: str  # the planted text: untrusted, shown as data
    reason: str
    created_at: datetime
    reviewed_at: datetime | None
    reviewed_by_name: str | None
    review_note: str | None


class ContentFlagReviewIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class RunMessageOut(BaseModel):
    run_id: uuid.UUID
    agent: str
    parent_run_id: uuid.UUID | None
    ordinal: int
    role: str
    segment: str | None
    content: dict[str, Any]
    created_at: datetime


@router.post("/conversations", status_code=status.HTTP_201_CREATED)
def create_conversation(body: ConversationIn, session: SessionDep, scope: ProjectScopeDep) -> ConversationOut:
    c = conversations.create_conversation(session, scope, agent=body.agent, model_id=body.model_id, title=body.title)
    return ConversationOut(id=c.id, model_id=c.model_id, agent=c.agent, title=c.title, created_at=c.created_at)


@router.get("/conversations")
def list_conversations(session: SessionDep, scope: ProjectScopeDep, model_id: uuid.UUID | None = None) -> list[ConversationOut]:
    return [ConversationOut(id=c.id, model_id=c.model_id, agent=c.agent, title=c.title, created_at=c.created_at)
            for c in conversations.list_conversations(session, scope, model_id)]


@router.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> list[MessageOut]:
    messages = conversations.list_messages(session, scope, conversation_id)
    runs = conversations.message_runs(session, scope, [m.run_id for m in messages if m.run_id])
    routes = conversations.routing_decisions(
        session, scope, [m.run_id for m in messages if m.run_id and str(m.role) == "user"])
    out = []
    for m in messages:
        r = runs.get(m.run_id)
        user = str(m.role) == "user"
        out.append(MessageOut(
            id=m.id, ordinal=m.ordinal, role=str(m.role), text=m.text, attachments=list(m.attachments or []),
            activity=list(m.activity or []), run_id=m.run_id,
            agent=r.agent if r and not user else None,
            turn_run_id=(r.parent_run_id or r.id) if r else None,
            route=_route_out(routes.get(m.run_id)) if user else None, created_at=m.created_at))
    return out


@router.get("/agent-runs/{run_id}")
def get_run(run_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> RunOut:
    r = conversations.get_run(session, scope, run_id)
    return RunOut(
        id=r.id, agent=r.agent, llm_model=r.llm_model, parent_run_id=r.parent_run_id,
        child_run_ids=[c.id for c in conversations.child_runs(session, scope, r.id)], status=str(r.status), status_detail=r.status_detail,
        iterations=r.iterations, tool_calls=r.tool_calls, input_tokens=r.input_tokens, output_tokens=r.output_tokens,
        cache_read_tokens=r.cache_read_tokens, cache_write_tokens=r.cache_write_tokens,
        changeset_seqs=list(r.changeset_seqs or []), started_at=r.started_at, finished_at=r.finished_at,
        route=_route_out(conversations.routing_decision(session, scope, r.id)),
    )


def _runners(app_state, settings, tenant_id: uuid.UUID, project_id: uuid.UUID) -> Callable[[str], Runner]:
    """Builds the runner for an agent. Building its model raises LLMError when it isn't
    configured; the Architect's helpers (Business Requirements consultations, the Validator)
    degrade instead, as the Architect expects."""
    factory = app_state.chat_model_factory

    def build(agent: str) -> Runner:
        chat_model = factory(agent)
        if agent != "architect":
            def run_requirements(run_id: uuid.UUID, emit, note: str | None = None) -> None:
                run_turn(
                    app_state.session_factory, tenant_id=tenant_id, project_id=project_id,
                    run_id=run_id, chat_model=chat_model, embedder=app_state.embedder,
                    max_iterations=settings.agent_max_iterations, emit=emit, checkpointer=_checkpointer(app_state),
                    store=app_state.object_store,
                )
            return Runner(chat_model.model, run_requirements)

        box = get_box() if settings.secrets_key else None
        try:  # drives the Business Requirements subgraph behind ask_requirements
            requirements_model = factory("business_requirements")
        except LLMError:
            requirements_model = None
        validator_model = validator_unavailable = None
        try:  # the Validator reviews the candidate on a different model family
            validator_model = factory("validator")
        except LLMError as exc:
            validator_unavailable = str(exc)

        def run_architect(run_id: uuid.UUID, emit, note: str | None = None) -> None:
            run_design(
                app_state.session_factory, tenant_id=tenant_id, project_id=project_id,
                run_id=run_id, chat_model=chat_model, embedder=app_state.embedder,
                max_iterations=settings.architect_max_iterations, emit=emit,
                connector=app_state.source_connector, box=box,
                checkpointer=_checkpointer(app_state), store=app_state.object_store,
                requirements_model=requirements_model, max_consultations=settings.architect_max_consultations,
                consultation_max_iterations=settings.consultation_max_iterations,
                validator_model=validator_model, validator_unavailable=validator_unavailable,
                validator_max_iterations=settings.validator_max_iterations,
                max_rounds=settings.validator_max_rounds, max_attempts=settings.validator_max_attempts_per_issue,
                tot=search.Settings(
                    children=settings.tot_children, beam=settings.tot_beam, threshold=settings.tot_score_threshold,
                    max_model_calls=settings.tot_max_model_calls, max_tool_calls=settings.tot_max_tool_calls,
                    max_seconds=settings.tot_max_seconds, max_output_tokens=settings.tot_max_output_tokens,
                ) if settings.architect_tot_enabled else None,
                max_decision_points=settings.architect_max_decision_points, handoff=note,
            )
        return Runner(chat_model.model, run_architect)

    return build


@router.post("/conversations/{conversation_id}/messages", response_class=StreamingResponse)
def send_message(
    conversation_id: uuid.UUID, body: MessageIn, request: Request, session: SessionDep, scope: ProjectScopeDep
) -> StreamingResponse:
    """Send a message and stream the reply. In an Assistant conversation the request router
    picks the agent (a `route` event says which and why), unless `agent` names it."""
    settings = get_settings()
    conversation = conversations.get_conversation(session, scope, conversation_id)
    app_state = request.app.state
    runners = _runners(app_state, settings, scope.tenant_id, scope.project_id)
    routed = conversation.agent == conversations.ROUTED
    # Build the model before storing anything, so a missing key fails the request cleanly.
    try:
        if routed:
            runner = None
            chat_model = None if body.agent else app_state.chat_model_factory("router")
            llm_model = chat_model.model if chat_model else "none (sent to a chosen agent)"
        else:
            runner = runners(conversation.agent)
            llm_model = runner.model
    except LLMError as exc:
        from app.domain.tenancy import InvalidOperation

        raise InvalidOperation(str(exc)) from exc
    run, redacted = conversations.start_turn(
        session, scope, conversation_id, text=body.text,
        attachment_version_ids=body.attachment_version_ids, llm_model=llm_model, target=body.agent,
        instead_of=body.instead_of_run_id,
    )
    session.commit()  # the run and user message must exist before the worker reads them

    events: queue.Queue = queue.Queue()
    tenant_id, project_id, run_id = scope.tenant_id, scope.project_id, run.id

    monitor_model = monitor_unavailable = None
    if settings.monitor_enabled:
        try:  # reads the finished run's transcript; another model family than the Architect
            monitor_model = app_state.chat_model_factory("monitor")
        except LLMError as exc:
            monitor_unavailable = str(exc)

    def worker() -> None:
        try:
            if routed:
                route_turn(app_state.session_factory, tenant_id=tenant_id, project_id=project_id, run_id=run_id,
                           chat_model=chat_model, target=body.agent, runners=runners, emit=events.put,
                           instead_of=body.instead_of_run_id)
            else:
                runner.run(run_id, events.put, None)
        except Exception as exc:  # the runs record their own outcomes; this catches setup failures
            traceback.print_exc()
            _mark_failed(app_state.session_factory, run_id, f"the agent could not start ({type(exc).__name__})")
            events.put({"type": "done", "status": "failed", "detail": "the agent could not start",
                        "message_id": None, "run": {"id": str(run_id)}})
        finally:
            events.put(None)  # the stream ends here; the monitor runs after, so nobody waits for it
        if settings.monitor_enabled:
            monitor_run(app_state.session_factory, tenant_id=tenant_id, project_id=project_id, run_id=run_id,
                        chat_model=monitor_model, settings=settings, unavailable=monitor_unavailable)

    # The agent runs in its own thread with its own session, independent of this request's
    # session, so it finishes and records its outcome even if the browser disconnects.
    threading.Thread(target=worker, name=f"agent-run-{run_id}", daemon=True).start()

    def stream() -> Iterator[str]:
        yield f"data: {json.dumps({'type': 'run', 'run_id': str(run_id), 'redacted_credentials': redacted})}\n\n"
        while (event := events.get()) is not None:
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/agent-runs/{run_id}/messages")
def get_run_messages(
    run_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep, include_children: bool = True,
) -> list[RunMessageOut]:
    """The run's transcript (system prompt, briefs, assistant text, thinking and tool calls,
    tool results), followed by those of the runs it started unless include_children is false."""
    return [
        RunMessageOut(run_id=r.id, agent=r.agent, parent_run_id=r.parent_run_id, ordinal=m.ordinal, role=m.role,
                      segment=m.segment, content=m.content, created_at=m.created_at)
        for r, m in conversations.run_transcript(session, scope, run_id, include_children)
    ]


def _names(session, ids) -> dict[uuid.UUID, str | None]:
    from app.persistence.models import User

    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u.display_name or u.email for u in session.scalars(select(User).where(User.id.in_(ids)))}


def _assessments_out(session, scope, rows) -> list[AssessmentOut]:
    reviews = conversations.assessment_reviews(session, scope, [a.id for a in rows])
    names = _names(session, [r.reviewed_by for rs in reviews.values() for r in rs])
    return [AssessmentOut(
        id=a.id, run_id=a.run_id, status=a.status, monitor_model=a.monitor_model, score=a.score, flagged=a.flagged,
        summary=a.summary, concerns=a.concerns, untrusted_instructions=a.untrusted_instructions, samples=a.samples,
        input_tokens=a.input_tokens, output_tokens=a.output_tokens, truncated=a.truncated, detail=a.detail,
        created_at=a.created_at, finished_at=a.finished_at,
        reviews=[AssessmentReviewOut(id=r.id, verdict=r.verdict, note=r.note, by_name=names.get(r.reviewed_by),
                                     created_at=r.created_at) for r in reviews.get(a.id, [])],
    ) for a in rows]


@router.get("/agent-runs/{run_id}/assessments")
def get_run_assessments(run_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> list[AssessmentOut]:
    """The transcript monitor's assessments of a top-level run, newest first. Pending while the
    monitor reads the run; skipped when no monitor model is configured. Needs edit access."""
    return _assessments_out(session, scope, conversations.run_assessments(session, scope, run_id))


@router.get("/conversations/{conversation_id}/assessments")
def get_conversation_assessments(conversation_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> list[AssessmentOut]:
    """The latest assessment of each run in a conversation, to show beside its replies."""
    return _assessments_out(session, scope, conversations.conversation_assessments(session, scope, conversation_id))


@router.post("/agent-runs/{run_id}/assessments/{assessment_id}/reviews", status_code=status.HTTP_201_CREATED)
def review_assessment(run_id: uuid.UUID, assessment_id: uuid.UUID, body: AssessmentReviewIn, session: SessionDep,
                      scope: ProjectScopeDep) -> AssessmentReviewOut:
    """A person's verdict on a monitor assessment: was the concern real? Append-only."""
    r = conversations.review_assessment(session, scope, run_id, assessment_id, verdict=body.verdict, note=body.note)
    session.commit()
    return AssessmentReviewOut(id=r.id, verdict=r.verdict, note=r.note, by_name=_names(session, [r.reviewed_by]).get(r.reviewed_by),
                               created_at=r.created_at)


def _flags_out(session, scope, rows) -> list[ContentFlagOut]:
    from app.persistence.documents import Document, DocumentVersion
    from app.persistence.sources import SourceConnection

    versions = {v.id: v for v in session.scalars(select(DocumentVersion).where(
        DocumentVersion.tenant_id == scope.tenant_id, DocumentVersion.project_id == scope.project_id,
        DocumentVersion.id.in_([f.document_version_id for f in rows if f.document_version_id])))}
    docs = {d.id: d for d in session.scalars(select(Document).where(
        Document.tenant_id == scope.tenant_id, Document.project_id == scope.project_id,
        Document.id.in_([v.document_id for v in versions.values()])))}
    srcs = {c.id: c for c in session.scalars(select(SourceConnection).where(
        SourceConnection.tenant_id == scope.tenant_id, SourceConnection.project_id == scope.project_id,
        SourceConnection.id.in_([f.source_id for f in rows if f.source_id])))}
    names = _names(session, [f.reviewed_by for f in rows])
    out = []
    for f in rows:
        v = versions.get(f.document_version_id)
        out.append(ContentFlagOut(
            id=f.id, run_id=f.run_id, agent=f.agent, target_kind=f.target_kind,
            document_id=v.document_id if v else None, document_version_id=f.document_version_id,
            document_title=docs[v.document_id].title if v and v.document_id in docs else None,
            version_no=v.version_no if v else None, location=f.location, source_id=f.source_id,
            source_name=srcs[f.source_id].name if f.source_id in srcs else None, source_table=f.source_table,
            source_column=f.source_column, quote=f.quote, reason=f.reason, created_at=f.created_at,
            reviewed_at=f.reviewed_at, reviewed_by_name=names.get(f.reviewed_by), review_note=f.review_note))
    return out


@router.get("/content-flags")
def list_content_flags(session: SessionDep, scope: ProjectScopeDep, include_reviewed: bool = False) -> list[ContentFlagOut]:
    """Untrusted content agents flagged for trying to instruct them, newest first."""
    return _flags_out(session, scope, flags.list_flags(session, scope, include_reviewed=include_reviewed))


@router.post("/content-flags/{flag_id}/review")
def review_content_flag(flag_id: uuid.UUID, body: ContentFlagReviewIn, session: SessionDep,
                        scope: ProjectScopeDep) -> ContentFlagOut:
    """A person marks a flag reviewed. Agents can't."""
    f = flags.review_flag(session, scope, flag_id, body.note)
    session.commit()
    return _flags_out(session, scope, [f])[0]
