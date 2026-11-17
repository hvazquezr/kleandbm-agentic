"""Conversations with agents, their messages, and agent runs (with usage) per message.

The transcript shown to users lives here. LangGraph checkpoints (run state for pause and
resume) live in their own tables, apart from these and from the registers (spec §7).
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.models import Base, _enum


class MessageRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"  # hit its execution budget, or the model refused


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_conversations_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE", name="fk_conversations_project",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "model_id"],
            ["data_models.tenant_id", "data_models.project_id", "data_models.id"],
            ondelete="CASCADE", name="fk_conversations_model",
        ),
        Index("ix_conversations_project_model", "project_id", "model_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    model_id: Mapped[uuid.UUID | None] = mapped_column()
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_agent_runs_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.project_id", "conversations.id"],
            ondelete="CASCADE", name="fk_agent_runs_conversation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "parent_run_id"],
            ["agent_runs.tenant_id", "agent_runs.project_id", "agent_runs.id"],
            ondelete="CASCADE", name="fk_agent_runs_parent",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    # Set for a subgraph run started by another agent's run (e.g. the Architect consulting the
    # Business Requirements agent), so usage and register changes stay attributed per agent.
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    llm_model: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus, "run_status"), nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text)
    initiated_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Changeset seqs this run wrote, for tracing register changes back to the run.
    changeset_seqs: Mapped[list[int]] = mapped_column(JSONB, nullable=False, server_default="[]")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.project_id", "conversations.id"],
            ondelete="CASCADE", name="fk_conversation_messages_conversation",
        ),
        UniqueConstraint("conversation_id", "ordinal", name="uq_conversation_messages_ordinal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[MessageRole] = mapped_column(_enum(MessageRole, "message_role"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Document versions the user attached to this message.
    attachments: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    # For assistant messages: what the agent did (tool activity summaries and changed records).
    activity: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    run_id: Mapped[uuid.UUID | None] = mapped_column()
    author_id: Mapped[uuid.UUID] = mapped_column(nullable=False)  # user, or the run for agents
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RunMessage(Base):
    """One entry of what an agent run's model saw and said, in order: the system prompt, the
    brief or user turn, assistant text, thinking and tool calls, and tool results. Append-only
    and written as the run goes, for every agent (chat, Architect, consultation, Validator and
    the Architect's design-search calls). Checkpoints stay runtime state for resuming a run;
    this is the readable record. Credentials are redacted before writing."""

    __tablename__ = "run_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.project_id", "agent_runs.id"],
            ondelete="CASCADE", name="fk_run_messages_run",
        ),
        UniqueConstraint("tenant_id", "project_id", "run_id", "ordinal", name="uq_run_messages_ordinal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # system | user | assistant | tool
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # Which conversation inside the run: null for the run's own loop, "history" for earlier
    # chat turns replayed to the model, "search:<decision point id>:<generator|evaluator>" for
    # design-search calls.
    segment: Mapped[str | None] = mapped_column(String(200))
    # system: {text, sha256} (text only the first time a prompt appears in the run);
    # user: {text}; assistant: {text, thinking, tool_calls, stop_reason, model};
    # tool: {results: [{id, content, is_error, image?}]}.
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RunAssessment(Base):
    """The transcript monitor's reading of one top-level run (its child runs included): a
    suspicion score for the agents' behaviour, the concerns behind it, and the instructions it
    found in untrusted content (documents, source values, comments). Triage for people, never a
    gate. One row per assessment; a run can be reassessed."""

    __tablename__ = "run_assessments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.project_id", "agent_runs.id"],
            ondelete="CASCADE", name="fk_run_assessments_run",
        ),
        Index("ix_run_assessments_run", "tenant_id", "project_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # pending | completed | failed | skipped (no monitor model configured)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    monitor_model: Mapped[str | None] = mapped_column(String(100))
    score: Mapped[int | None] = mapped_column(Integer)  # 0-100, the mean over samples
    flagged: Mapped[bool] = mapped_column(nullable=False, default=False)
    summary: Mapped[str | None] = mapped_column(Text)
    # [{kind, explanation, ordinals: [{agent, ordinal}]}]
    concerns: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    # [{agent, ordinal, quote, followed}]
    untrusted_instructions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    samples: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)  # each sample's score
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    truncated: Mapped[bool] = mapped_column(nullable=False, default=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
