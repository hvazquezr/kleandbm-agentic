"""Conversations with agents. Talking to an agent needs PROJECT_EDIT, because the agent writes
to the registers on the user's behalf."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain import documents, modeling
from app.domain.access import NotFoundInScope, Permission, ProjectScope
from app.domain.redaction import redact_credentials
from app.domain.tenancy import InvalidOperation
from app.persistence.conversations import AgentRun, Conversation, ConversationMessage, MessageRole, RunStatus

AGENTS = {"business_requirements": "Requirements", "architect": "Architect"}


def _user(scope: ProjectScope) -> uuid.UUID:
    return scope.actor.on_behalf_of or scope.actor.id


def create_conversation(
    session: Session, scope: ProjectScope, *, agent: str, model_id: uuid.UUID | None, title: str | None
) -> Conversation:
    scope.require(Permission.PROJECT_EDIT)
    if agent not in AGENTS:
        raise InvalidOperation(f"unknown agent {agent!r}")
    if agent == "architect" and model_id is None:
        raise InvalidOperation("an Architect conversation belongs to a data model")
    if model_id is not None:
        modeling.get_model(session, scope, model_id)
    conversation = Conversation(
        tenant_id=scope.tenant_id, project_id=scope.project_id, model_id=model_id, agent=agent,
        title=(title or AGENTS[agent]).strip()[:255], created_by=_user(scope),
    )
    session.add(conversation)
    session.flush()
    return conversation


def list_conversations(session: Session, scope: ProjectScope, model_id: uuid.UUID | None = None) -> list[Conversation]:
    scope.require(Permission.PROJECT_READ)
    query = select(Conversation).where(
        Conversation.tenant_id == scope.tenant_id, Conversation.project_id == scope.project_id
    )
    if model_id is not None:
        query = query.where(Conversation.model_id == model_id)
    return list(session.scalars(query.order_by(Conversation.created_at.desc())))


def get_conversation(session: Session, scope: ProjectScope, conversation_id: uuid.UUID) -> Conversation:
    scope.require(Permission.PROJECT_READ)
    conversation = session.scalar(
        select(Conversation).where(
            Conversation.tenant_id == scope.tenant_id,
            Conversation.project_id == scope.project_id,
            Conversation.id == conversation_id,
        )
    )
    if conversation is None:
        raise NotFoundInScope("conversation")
    return conversation


def list_messages(session: Session, scope: ProjectScope, conversation_id: uuid.UUID) -> list[ConversationMessage]:
    conversation = get_conversation(session, scope, conversation_id)
    return list(session.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation.id)
        .order_by(ConversationMessage.ordinal)
    ))


def start_turn(
    session: Session,
    scope: ProjectScope,
    conversation_id: uuid.UUID,
    *,
    text: str,
    attachment_version_ids: list[uuid.UUID],
    llm_model: str,
) -> tuple[AgentRun, bool]:
    """Store the user's message and create the run that will answer it. Returns the run and
    whether credentials were redacted from the message."""
    scope.require(Permission.PROJECT_EDIT)
    conversation = get_conversation(session, scope, conversation_id)
    text, redacted = redact_credentials(text)  # before it's stored or sent anywhere
    if not text.strip() and not attachment_version_ids:
        raise InvalidOperation("the message is empty")
    running = session.scalar(
        select(func.count()).select_from(AgentRun)
        .where(AgentRun.conversation_id == conversation.id, AgentRun.status == RunStatus.RUNNING)
    )
    if running:
        raise InvalidOperation("the agent is still answering the previous message")
    for vid in attachment_version_ids:
        documents.get_version(session, scope, vid)  # attachments must be this project's documents

    run = AgentRun(
        tenant_id=scope.tenant_id, project_id=scope.project_id, conversation_id=conversation.id,
        agent=conversation.agent, llm_model=llm_model, status=RunStatus.RUNNING, initiated_by=_user(scope),
        changeset_seqs=[],
    )
    session.add(run)
    session.flush()
    next_ordinal = (session.scalar(
        select(func.max(ConversationMessage.ordinal)).where(ConversationMessage.conversation_id == conversation.id)
    ) or 0) + 1
    session.add(ConversationMessage(
        tenant_id=scope.tenant_id, project_id=scope.project_id, conversation_id=conversation.id,
        ordinal=next_ordinal, role=MessageRole.USER,
        text=text.strip() or "(attached documents)", attachments=[str(v) for v in attachment_version_ids],
        activity=[], run_id=run.id, author_id=_user(scope),
    ))
    session.flush()
    return run, redacted


def get_run(session: Session, scope: ProjectScope, run_id: uuid.UUID) -> AgentRun:
    scope.require(Permission.PROJECT_READ)
    run = session.scalar(
        select(AgentRun).where(
            AgentRun.tenant_id == scope.tenant_id, AgentRun.project_id == scope.project_id, AgentRun.id == run_id
        )
    )
    if run is None:
        raise NotFoundInScope("agent run")
    return run


def child_runs(session: Session, scope: ProjectScope, run_id: uuid.UUID) -> list[AgentRun]:
    """Consultation runs started by a run, oldest first."""
    scope.require(Permission.PROJECT_READ)
    return list(session.scalars(
        select(AgentRun).where(AgentRun.tenant_id == scope.tenant_id, AgentRun.project_id == scope.project_id,
                               AgentRun.parent_run_id == run_id).order_by(AgentRun.started_at)
    ))
