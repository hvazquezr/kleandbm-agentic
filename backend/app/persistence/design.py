"""Design candidates: the Architect's isolated workspace.

The Architect builds a proposed model here instead of editing the live model element by
element. The candidate pins the watermark it started from (`base_seq`). Applying it writes
one attributed changeset; if the model changed after `base_seq`, the candidate is stale and
is not applied (spec §7, stale detection).
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.models import Base, _enum


class CandidateStatus(enum.StrEnum):
    BUILDING = "building"  # the Architect is still working on it
    PROPOSED = "proposed"  # finished; waiting for a person to apply it (the model had content)
    APPLIED = "applied"
    DISCARDED = "discarded"
    STALE = "stale"  # the model changed after base_seq, so it can't be applied as is
    FAILED = "failed"  # the run ended before the candidate was finished


class DesignCandidate(Base):
    __tablename__ = "design_candidates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_design_candidates_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "model_id"],
            ["data_models.tenant_id", "data_models.project_id", "data_models.id"],
            ondelete="CASCADE", name="fk_design_candidates_model",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.project_id", "agent_runs.id"],
            ondelete="CASCADE", name="fk_design_candidates_run",
        ),
        Index("ix_design_candidates_model", "project_id", "model_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    model_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column()
    status: Mapped[CandidateStatus] = mapped_column(_enum(CandidateStatus, "candidate_status"), nullable=False)
    # The project watermark the candidate was built from.
    base_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # Tables (with attributes), relationships, requirement mappings and decision links.
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The Architect's own structural checks, recomputed whenever the candidate changes.
    internal_checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    summary: Mapped[str | None] = mapped_column(Text)
    applied_seq: Mapped[int | None] = mapped_column(Integer)
    applied_by: Mapped[uuid.UUID | None] = mapped_column()
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
