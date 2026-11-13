"""Data models, versioned records, changesets, links, layout, versions, and lifecycle events.

Versioning (spec §7, "Version history mechanism"):
- ``records`` holds one identity row per model element or register entry.
- Each change appends a row to that type's ``*_revisions`` table, keyed by (record, seq).
  Revisions are never updated or deleted. A delete is a revision with ``is_deleted``.
- ``seq`` is the per-project changeset sequence. A model version is a seq watermark, and
  the state "as of" seq W is each record's latest revision with seq <= W.
- Canvas positions (``element_layouts``) are cosmetic and not versioned.

Every table carries tenant_id and project_id, and every reference uses a composite foreign
key that includes them, so nothing can point across projects.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.domain.access import ActorKind
from app.domain.records import (
    Additivity,
    AttributeRole,
    Cardinality,
    CheckStatus,
    CheckType,
    ConfirmationStatus,
    DecisionKind,
    DecisionStatus,
    DefinitionKind,
    DefinitionProvenance,
    FeasibilityStatus,
    LinkType,
    LogicalType,
    ModelState,
    OriginKind,
    QuestionKind,
    QuestionStatus,
    RecordType,
    ScdType,
    TableKind,
)
from app.persistence.models import Base, _enum


def _project_fk(name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "project_id"],
        ["projects.tenant_id", "projects.id"],
        ondelete="CASCADE",
        name=name,
    )


def _record_fk(column: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "project_id", column],
        ["records.tenant_id", "records.project_id", "records.id"],
        ondelete="CASCADE",
        name=name,
    )


def _changeset_fk(column: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "project_id", column],
        ["changesets.tenant_id", "changesets.project_id", "changesets.seq"],
        ondelete="CASCADE",
        name=name,
    )


def _model_fk(name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "project_id", "model_id"],
        ["data_models.tenant_id", "data_models.project_id", "data_models.id"],
        ondelete="CASCADE",
        name=name,
    )


class DataModel(Base):
    __tablename__ = "data_models"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_data_models_scope"),
        _project_fk("fk_data_models_project"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProjectSequence(Base):
    """The last changeset seq issued per project. Its row lock serializes changesets."""

    __tablename__ = "project_sequences"
    __table_args__ = (_project_fk("fk_project_sequences_project"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class Changeset(Base):
    __tablename__ = "changesets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "seq", name="uq_changesets_scope_seq"),
        _project_fk("fk_changesets_project"),
        _model_fk("fk_changesets_model"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Set when the changeset touches model-scoped records; null for register-only changes.
    model_id: Mapped[uuid.UUID | None] = mapped_column()
    actor_kind: Mapped[ActorKind] = mapped_column(_enum(ActorKind, "actor_kind"), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    on_behalf_of: Mapped[uuid.UUID | None] = mapped_column()
    reason: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Record(Base):
    """Stable identity of a model element or register entry."""

    __tablename__ = "records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_records_scope"),
        UniqueConstraint("project_id", "record_type", "code", name="uq_records_code"),
        _project_fk("fk_records_project"),
        _model_fk("fk_records_model"),
        _changeset_fk("created_seq", "fk_records_created_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    model_id: Mapped[uuid.UUID | None] = mapped_column()
    record_type: Mapped[RecordType] = mapped_column(
        _enum(RecordType, "record_type"), nullable=False
    )
    code: Mapped[str | None] = mapped_column(String(32))
    created_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)


class RevisionMixin:
    """Columns shared by every ``*_revisions`` table."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    record_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        t = cls.__tablename__
        return (
            _record_fk("record_id", f"fk_{t}_record"),
            _changeset_fk("seq", f"fk_{t}_seq"),
            *getattr(cls, "__extra_args__", ()),
        )


class TableRevision(RevisionMixin, Base):
    __tablename__ = "table_revisions"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[TableKind] = mapped_column(_enum(TableKind, "table_kind"), nullable=False)
    grain: Mapped[str | None] = mapped_column(String(2000))
    description: Mapped[str | None] = mapped_column(Text)
    scd_type: Mapped[ScdType] = mapped_column(_enum(ScdType, "scd_type"), nullable=False)


class AttributeRevision(RevisionMixin, Base):
    __tablename__ = "attribute_revisions"
    __extra_args__ = (_record_fk("table_id", "fk_attribute_revisions_table"),)

    table_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    logical_type: Mapped[LogicalType] = mapped_column(
        _enum(LogicalType, "logical_type"), nullable=False
    )
    role: Mapped[AttributeRole] = mapped_column(
        _enum(AttributeRole, "attribute_role"), nullable=False
    )
    is_nullable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    additivity: Mapped[Additivity | None] = mapped_column(_enum(Additivity, "additivity"))
    description: Mapped[str | None] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class RelationshipRevision(RevisionMixin, Base):
    __tablename__ = "relationship_revisions"
    __extra_args__ = (
        _record_fk("from_table_id", "fk_relationship_revisions_from_table"),
        _record_fk("from_attribute_id", "fk_relationship_revisions_from_attribute"),
        _record_fk("to_table_id", "fk_relationship_revisions_to_table"),
        _record_fk("to_attribute_id", "fk_relationship_revisions_to_attribute"),
    )

    from_table_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    from_attribute_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    to_table_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    to_attribute_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    cardinality: Mapped[Cardinality] = mapped_column(
        _enum(Cardinality, "cardinality"), nullable=False
    )
    role_name: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)


class RequirementRevision(RevisionMixin, Base):
    __tablename__ = "requirement_revisions"

    statement: Mapped[str] = mapped_column(Text, nullable=False)
    origin_kind: Mapped[OriginKind] = mapped_column(
        _enum(OriginKind, "origin_kind"), nullable=False
    )
    origin_ref: Mapped[str | None] = mapped_column(String(2000))
    confirmation_status: Mapped[ConfirmationStatus] = mapped_column(
        _enum(ConfirmationStatus, "confirmation_status"), nullable=False
    )
    feasibility_status: Mapped[FeasibilityStatus] = mapped_column(
        _enum(FeasibilityStatus, "feasibility_status"), nullable=False
    )
    feasibility_notes: Mapped[str | None] = mapped_column(Text)
    is_proposed_alternative: Mapped[bool] = mapped_column(Boolean, nullable=False)


class DefinitionRevision(RevisionMixin, Base):
    __tablename__ = "definition_revisions"

    term: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    kind: Mapped[DefinitionKind] = mapped_column(
        _enum(DefinitionKind, "definition_kind"), nullable=False
    )
    definition: Mapped[str | None] = mapped_column(Text)
    business_scope: Mapped[str | None] = mapped_column(String(2000))
    metric: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    provenance: Mapped[DefinitionProvenance] = mapped_column(
        _enum(DefinitionProvenance, "definition_provenance"), nullable=False
    )
    origin_ref: Mapped[str | None] = mapped_column(String(2000))
    confirmation_status: Mapped[ConfirmationStatus] = mapped_column(
        _enum(ConfirmationStatus, "confirmation_status"), nullable=False
    )


class QuestionRevision(RevisionMixin, Base):
    __tablename__ = "question_revisions"

    kind: Mapped[QuestionKind] = mapped_column(_enum(QuestionKind, "question_kind"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    impact: Mapped[str | None] = mapped_column(Text)
    is_blocking: Mapped[bool] = mapped_column(Boolean, nullable=False)
    proposed_action: Mapped[str | None] = mapped_column(Text)
    status: Mapped[QuestionStatus] = mapped_column(
        _enum(QuestionStatus, "question_status"), nullable=False
    )
    resolution: Mapped[str | None] = mapped_column(Text)


class DecisionRevision(RevisionMixin, Base):
    __tablename__ = "decision_revisions"

    kind: Mapped[DecisionKind] = mapped_column(_enum(DecisionKind, "decision_kind"), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    alternatives: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    status: Mapped[DecisionStatus] = mapped_column(
        _enum(DecisionStatus, "decision_status"), nullable=False
    )


class CheckRevision(RevisionMixin, Base):
    __tablename__ = "check_revisions"

    check_type: Mapped[CheckType] = mapped_column(_enum(CheckType, "check_type"), nullable=False)
    status: Mapped[CheckStatus] = mapped_column(_enum(CheckStatus, "check_status"), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    impact: Mapped[str | None] = mapped_column(Text)
    recommended_action: Mapped[str | None] = mapped_column(Text)
    based_on_seq: Mapped[int | None] = mapped_column(BigInteger)
    issue_key: Mapped[str | None] = mapped_column(String(200))
    is_blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    candidate_id: Mapped[uuid.UUID | None] = mapped_column()


REVISION_TABLES: dict[RecordType, type[RevisionMixin]] = {
    RecordType.TABLE: TableRevision,
    RecordType.ATTRIBUTE: AttributeRevision,
    RecordType.RELATIONSHIP: RelationshipRevision,
    RecordType.REQUIREMENT: RequirementRevision,
    RecordType.DEFINITION: DefinitionRevision,
    RecordType.QUESTION: QuestionRevision,
    RecordType.DECISION: DecisionRevision,
    RecordType.CHECK: CheckRevision,
}


class RecordLink(Base):
    """A traceability link, live from ``created_seq`` until ``removed_seq`` (if set)."""

    __tablename__ = "record_links"
    __table_args__ = (
        _project_fk("fk_record_links_project"),
        _record_fk("from_id", "fk_record_links_from"),
        _record_fk("to_id", "fk_record_links_to"),
        _changeset_fk("created_seq", "fk_record_links_created_seq"),
        _changeset_fk("removed_seq", "fk_record_links_removed_seq"),
        Index(
            "uq_record_links_live",
            "from_id",
            "to_id",
            "link_type",
            unique=True,
            postgresql_where=text("removed_seq IS NULL"),
        ),
        Index("ix_record_links_to_id", "to_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    from_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    to_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    link_type: Mapped[LinkType] = mapped_column(_enum(LinkType, "link_type"), nullable=False)
    created_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    removed_seq: Mapped[int | None] = mapped_column(BigInteger)


class ElementLayout(Base):
    """Canvas position of a model element. Cosmetic: not versioned, no changeset."""

    __tablename__ = "element_layouts"
    __table_args__ = (
        _model_fk("fk_element_layouts_model"),
        _record_fk("record_id", "fk_element_layouts_record"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    model_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    record_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ModelVersion(Base):
    """A named version: a label on a changeset watermark."""

    __tablename__ = "model_versions"
    __table_args__ = (
        _model_fk("fk_model_versions_model"),
        _changeset_fk("seq", "fk_model_versions_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    model_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ModelStateEvent(Base):
    """Append-only lifecycle history (Draft, Ready for Review, Accepted) against a watermark."""

    __tablename__ = "model_state_events"
    __table_args__ = (
        _model_fk("fk_model_state_events_model"),
        _changeset_fk("seq", "fk_model_state_events_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    model_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    # Watermark the state applies to. Null only for the initial Draft of an empty project.
    seq: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[ModelState] = mapped_column(_enum(ModelState, "model_state"), nullable=False)
    actor_kind: Mapped[ActorKind] = mapped_column(_enum(ActorKind, "actor_kind"), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    on_behalf_of: Mapped[uuid.UUID | None] = mapped_column()
    note: Mapped[str | None] = mapped_column(String(4000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
