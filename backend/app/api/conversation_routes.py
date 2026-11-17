"""Conversations with agents. Sending a message streams the agent's reply as Server-Sent
Events:

  data: {"type": "run", "run_id": ...}
  data: {"type": "text", "text": "..."}                       (incremental reply text)
  data: {"type": "tool", "status": "started|ok|error", "name": ..., "summary": ..., "record": ...}
  data: {"type": "done", "status": ..., "message_id": ..., "run": {usage...}}
"""

import json
import queue
import threading
import traceback
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.adapters.llm import LLMError
from app.agents.architect import search
from app.agents.architect.agent import run_design
from app.agents.monitor.monitor import monitor_run
from app.agents.requirements.agent import run_turn
from app.api.source_routes import get_box
from app.api.deps import ProjectScopeDep, SessionDep
from app.config import get_settings
from app.persistence.conversations import AgentRun, RunStatus
from app.domain import conversations

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
    model_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=255)
    agent: str = "business_requirements"


class ConversationOut(BaseModel):
    id: uuid.UUID
    model_id: uuid.UUID | None
    agent: str
    title: str
    created_at: datetime


class MessageIn(BaseModel):
    text: str = Field(default="", max_length=20_000)
    attachment_version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)


class MessageOut(BaseModel):
    id: uuid.UUID
    ordinal: int
    role: str
    text: str
    attachments: list[str]
    activity: list[dict[str, Any]]
    run_id: uuid.UUID | None
    created_at: datetime


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
    return [
        MessageOut(id=m.id, ordinal=m.ordinal, role=str(m.role), text=m.text, attachments=list(m.attachments or []),
                   activity=list(m.activity or []), run_id=m.run_id, created_at=m.created_at)
        for m in conversations.list_messages(session, scope, conversation_id)
    ]


@router.get("/agent-runs/{run_id}")
def get_run(run_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> RunOut:
    r = conversations.get_run(session, scope, run_id)
    return RunOut(
        id=r.id, agent=r.agent, llm_model=r.llm_model, parent_run_id=r.parent_run_id,
        child_run_ids=[c.id for c in conversations.child_runs(session, scope, r.id)], status=str(r.status), status_detail=r.status_detail,
        iterations=r.iterations, tool_calls=r.tool_calls, input_tokens=r.input_tokens, output_tokens=r.output_tokens,
        cache_read_tokens=r.cache_read_tokens, cache_write_tokens=r.cache_write_tokens,
        changeset_seqs=list(r.changeset_seqs or []), started_at=r.started_at, finished_at=r.finished_at,
    )


@router.post("/conversations/{conversation_id}/messages", response_class=StreamingResponse)
def send_message(
    conversation_id: uuid.UUID, body: MessageIn, request: Request, session: SessionDep, scope: ProjectScopeDep
) -> StreamingResponse:
    settings = get_settings()
    conversation = conversations.get_conversation(session, scope, conversation_id)
    # Build the model before storing anything, so a missing key fails the request cleanly.
    try:
        chat_model = request.app.state.chat_model_factory(conversation.agent)
    except LLMError as exc:
        from app.domain.tenancy import InvalidOperation

        raise InvalidOperation(str(exc)) from exc
    run, redacted = conversations.start_turn(
        session, scope, conversation_id, text=body.text,
        attachment_version_ids=body.attachment_version_ids, llm_model=chat_model.model,
    )
    session.commit()  # the run and user message must exist before the worker reads them

    events: queue.Queue = queue.Queue()
    app_state = request.app.state
    tenant_id, project_id, run_id = scope.tenant_id, scope.project_id, run.id

    agent = conversation.agent
    box = get_box() if agent == "architect" and settings.secrets_key else None
    requirements_model = validator_model = validator_unavailable = None
    if agent == "architect":
        factory = request.app.state.chat_model_factory
        try:  # drives the Business Requirements subgraph behind ask_requirements
            requirements_model = factory("business_requirements")
        except LLMError:
            requirements_model = None
        try:  # the Validator reviews the candidate on a different model family
            validator_model = factory("validator")
        except LLMError as exc:
            validator_unavailable = str(exc)

    monitor_model = monitor_unavailable = None
    if settings.monitor_enabled:
        try:  # reads the finished run's transcript; another model family than the Architect
            monitor_model = request.app.state.chat_model_factory("monitor")
        except LLMError as exc:
            monitor_unavailable = str(exc)

    def worker() -> None:
        try:
            if agent == "architect":
                run_design(
                    app_state.session_factory, tenant_id=tenant_id, project_id=project_id, run_id=run_id,
                    chat_model=chat_model, embedder=app_state.embedder,
                    max_iterations=settings.architect_max_iterations, emit=events.put,
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
                    max_decision_points=settings.architect_max_decision_points,
                )
            else:
                run_turn(
                    app_state.session_factory, tenant_id=tenant_id, project_id=project_id, run_id=run_id,
                    chat_model=chat_model, embedder=app_state.embedder, max_iterations=settings.agent_max_iterations,
                    emit=events.put, checkpointer=_checkpointer(app_state), store=app_state.object_store,
                )
        except Exception as exc:  # run_turn records its own outcomes; this catches setup failures
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


@router.get("/agent-runs/{run_id}/assessments")
def get_run_assessments(run_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> list[AssessmentOut]:
    """The transcript monitor's assessments of a top-level run, newest first. Pending while the
    monitor reads the run; skipped when no monitor model is configured."""
    return [AssessmentOut(
        id=a.id, run_id=a.run_id, status=a.status, monitor_model=a.monitor_model, score=a.score, flagged=a.flagged,
        summary=a.summary, concerns=a.concerns, untrusted_instructions=a.untrusted_instructions, samples=a.samples,
        input_tokens=a.input_tokens, output_tokens=a.output_tokens, truncated=a.truncated, detail=a.detail,
        created_at=a.created_at, finished_at=a.finished_at,
    ) for a in conversations.run_assessments(session, scope, run_id)]
