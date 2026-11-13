"""Project documents, their immutable versions, and extracted passages with embeddings.

Same isolation pattern as the rest of the schema: every row carries tenant_id and
project_id, and child rows reference their parent through a composite foreign key that
includes both.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
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

# Vector width of the embedding column. Must match Settings.embedding_dimensions.
EMBEDDING_DIMENSIONS = 1536


class VersionStatus(enum.StrEnum):
    PROCESSING = "processing"
    READY = "ready"  # text extracted and embedded; searchable
    STORED_ONLY = "stored_only"  # kept for download; no text extracted (e.g. images)
    EXTRACTION_FAILED = "extraction_failed"
    EMBEDDING_FAILED = "embedding_failed"  # passages exist without vectors; re-embed later


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_documents_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE", name="fk_documents_project",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_document_versions_scope"),
        UniqueConstraint("document_id", "version_no", name="uq_document_versions_number"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "document_id"],
            ["documents.tenant_id", "documents.project_id", "documents.id"],
            ondelete="CASCADE", name="fk_document_versions_document",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    # Display name only; never used to build paths.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[VersionStatus] = mapped_column(_enum(VersionStatus, "version_status"), nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    embedding_model: Mapped[str | None] = mapped_column(String(127))
    uploaded_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DocumentPassage(Base):
    __tablename__ = "document_passages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "version_id"],
            ["document_versions.tenant_id", "document_versions.project_id", "document_versions.id"],
            ondelete="CASCADE", name="fk_document_passages_version",
        ),
        UniqueConstraint("version_id", "ordinal", name="uq_document_passages_ordinal"),
        Index(
            "ix_document_passages_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
