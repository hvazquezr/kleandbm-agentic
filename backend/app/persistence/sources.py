"""Source connections (with encrypted credentials) and the evidence their tools return."""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
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

from app.domain.access import ActorKind
from app.persistence.models import Base, _enum


class SourceKind(enum.StrEnum):
    MYSQL = "mysql"


class SourceConnection(Base):
    __tablename__ = "source_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_source_connections_scope"),
        UniqueConstraint("project_id", "name", name="uq_source_connections_name"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE", name="fk_source_connections_project",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[SourceKind] = mapped_column(_enum(SourceKind, "source_kind"), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    database: Mapped[str] = mapped_column(String(128), nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    # Fernet ciphertext. Never returned by the API and never logged.
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    last_schema_fingerprint: Mapped[str | None] = mapped_column(String(64))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Result of the latest read-only privilege check (grants, problems, warnings).
    privilege_check: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Disabled connections are kept (their evidence must stay traceable) but can't be used.
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ModelSource(Base):
    """Which sources a data model draws from. The agents investigate only these."""

    __tablename__ = "model_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "model_id"],
            ["data_models.tenant_id", "data_models.project_id", "data_models.id"],
            ondelete="CASCADE", name="fk_model_sources_model",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "connection_id"],
            ["source_connections.tenant_id", "source_connections.project_id", "source_connections.id"],
            ondelete="CASCADE", name="fk_model_sources_connection",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    model_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    linked_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SourceEvidence(Base):
    """One tool call against a source, with its exact result. Append-only."""

    __tablename__ = "source_evidence"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_source_evidence_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "connection_id"],
            ["source_connections.tenant_id", "source_connections.project_id", "source_connections.id"],
            ondelete="CASCADE", name="fk_source_evidence_connection",
        ),
        Index("ix_source_evidence_connection_created", "connection_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Ties the evidence to the source schema it was observed on (spec: stale evidence detection).
    schema_fingerprint: Mapped[str | None] = mapped_column(String(64))
    purpose: Mapped[str | None] = mapped_column(String(500))
    actor_kind: Mapped[ActorKind] = mapped_column(_enum(ActorKind, "actor_kind"), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    on_behalf_of: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
