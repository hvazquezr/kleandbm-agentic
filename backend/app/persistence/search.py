"""Selective Tree of Thought: decision points and their search nodes (spec §7).

A decision point is a technical conflict the Architect recorded with at least two viable
alternatives and a trigger reason. Its search explores alternatives locally around the
affected elements in isolated node workspaces; nodes are inspectable design artifacts
(content, checks, scores, pruning reasons), never private reasoning transcripts. Nothing here
is authoritative: the selected node's design goes into the run's design candidate, and the
outcome is recorded as a decision in the register.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.models import Base, _enum


class Trigger(enum.StrEnum):
    """Technical triggers only: business-meaning conflicts go to people, never to search."""

    GRAIN_CONFLICT = "grain_conflict"
    COMPETING_REQUIREMENTS = "competing_requirements"
    AMBIGUOUS_RELATIONSHIP = "ambiguous_relationship"
    MULTIPLE_REMEDIATIONS = "multiple_remediations"


class SearchStatus(enum.StrEnum):
    SEARCHING = "searching"
    SELECTED = "selected"  # a completed candidate without failed hard checks was selected
    PARTIAL = "partial"  # only a partial or flawed candidate was available; the model stays Draft
    NO_CANDIDATE = "no_candidate"  # every alternative was rejected; the candidate is unchanged
    FAILED = "failed"


class NodeStatus(enum.StrEnum):
    ROOT = "root"
    KEPT = "kept"  # in the beam at its level
    PRUNED = "pruned"  # eligible but outside the beam, or below the score threshold
    REJECTED = "rejected"  # a demonstrated hard-constraint violation or an out-of-scope edit
    DUPLICATE = "duplicate"
    SELECTED = "selected"


class DecisionPoint(Base):
    __tablename__ = "decision_points"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_decision_points_scope"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "candidate_id"],
            ["design_candidates.tenant_id", "design_candidates.project_id", "design_candidates.id"],
            ondelete="CASCADE", name="fk_decision_points_candidate",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.project_id", "agent_runs.id"],
            ondelete="CASCADE", name="fk_decision_points_run",
        ),
        Index("ix_decision_points_candidate", "project_id", "candidate_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    model_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    trigger: Mapped[Trigger] = mapped_column(_enum(Trigger, "search_trigger"), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # {"elements": [...], "requirement_ids": [...], "stages": [...], "issue_key": str|None,
    #  "why": str, "alternatives": [{"choice", "rationale"}], "base_seq": int}
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[SearchStatus] = mapped_column(_enum(SearchStatus, "search_status"), nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(200))
    selected_node_id: Mapped[uuid.UUID | None] = mapped_column()
    decision_id: Mapped[uuid.UUID | None] = mapped_column()  # the decision register entry
    # {"model_calls", "tool_calls", "input_tokens", "output_tokens", "seconds"} and the budgets.
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SearchNode(Base):
    __tablename__ = "search_nodes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "decision_point_id"],
            ["decision_points.tenant_id", "decision_points.project_id", "decision_points.id"],
            ondelete="CASCADE", name="fk_search_nodes_decision_point",
        ),
        Index("ix_search_nodes_decision_point", "decision_point_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    decision_point_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column()
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32))  # grain | relationships | measures
    choice: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    assumptions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    # The node's full workspace (the candidate content with this branch's choices applied).
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    internal_checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    # {"scores": {...}, "total", "hard_violation", "unverified_critical", "complexity", "assessment"}
    evaluation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[NodeStatus] = mapped_column(_enum(NodeStatus, "search_node_status"), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)  # why it was kept, pruned, rejected or selected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
