"""Design candidates: review what the Architect proposes, then apply or discard it."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import ProjectScopeDep, SessionDep
from app.domain import design
from sqlalchemy import select

from app.persistence.design import CandidateStatus, DesignCandidate
from app.persistence.search import DecisionPoint, SearchNode

router = APIRouter(prefix="/tenants/{tenant_id}/projects/{project_id}", tags=["design"])


class CandidateSummary(BaseModel):
    id: uuid.UUID
    model_id: uuid.UUID
    run_id: uuid.UUID | None
    status: str
    base_seq: int
    summary: str | None
    tables: int
    hard_failures: int
    applied_seq: int | None
    created_at: datetime


class CandidateDiff(BaseModel):
    added: list[str]
    changed: list[str]
    removed: list[str]


class CandidateOut(CandidateSummary):
    content: dict[str, Any]
    internal_checks: list[dict[str, Any]]
    # Validator review rounds (findings with status, evidence, impact, recommended action) and
    # correction attempts per issue key.
    validation: list[dict[str, Any]]
    issue_attempts: dict[str, int]
    # Whether applying it would make the model Ready for Review, and what blocks that.
    ready_for_review: bool
    blockers: list[str]
    # What applying would change in the live model; null once applied or discarded.
    diff: CandidateDiff | None
    stale: bool


def _summary(c: DesignCandidate) -> dict[str, Any]:
    return dict(
        id=c.id, model_id=c.model_id, run_id=c.run_id, status=str(c.status), base_seq=c.base_seq,
        summary=c.summary, tables=len(c.content.get("tables", [])),
        hard_failures=len(design.hard_failures(c.internal_checks or [])), applied_seq=c.applied_seq,
        created_at=c.created_at,
    )


def _full(session, scope, c: DesignCandidate) -> CandidateOut:
    open_ = c.status in (CandidateStatus.BUILDING, CandidateStatus.PROPOSED)
    stale = open_ and design.is_stale(session, scope, c)
    diff = None
    if open_ and not stale:
        _, d = design.plan_changes(session, scope, c)
        diff = CandidateDiff(**d)
    ready, blockers = design.readiness(c)
    return CandidateOut(**_summary(c), content=c.content, internal_checks=c.internal_checks or [],
                        validation=c.validation or [], issue_attempts=c.issue_attempts or {},
                        ready_for_review=ready, blockers=blockers, diff=diff, stale=stale)


@router.get("/models/{model_id}/candidates")
def list_candidates(model_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> list[CandidateSummary]:
    return [CandidateSummary(**_summary(c)) for c in design.list_candidates(session, scope, model_id)]


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> CandidateOut:
    return _full(session, scope, design.get_candidate(session, scope, candidate_id))


@router.post("/candidates/{candidate_id}/apply")
def apply_candidate(candidate_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> CandidateOut:
    """A person applies a proposed design as one changeset. Refused (409) if the model changed since."""
    candidate = design.apply_candidate(session, scope, candidate_id)
    return _full(session, scope, candidate)


@router.post("/candidates/{candidate_id}/discard")
def discard_candidate(candidate_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> CandidateOut:
    return _full(session, scope, design.discard_candidate(session, scope, candidate_id))


class SearchNodeOut(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    depth: int
    stage: str | None
    choice: str
    rationale: str | None
    evidence_ids: list[str]
    assumptions: list[str]
    status: str
    reason: str | None
    evaluation: dict[str, Any]
    tables: list[str]  # the node's affected tables, by name (its full content stays server-side)


class DecisionPointOut(BaseModel):
    id: uuid.UUID
    trigger: str
    question: str
    scope: dict[str, Any]
    status: str
    stop_reason: str | None
    selected_node_id: uuid.UUID | None
    decision_id: uuid.UUID | None
    usage: dict[str, Any]
    created_at: datetime
    finished_at: datetime | None
    nodes: list[SearchNodeOut]


@router.get("/candidates/{candidate_id}/decision-points")
def list_decision_points(candidate_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> list[DecisionPointOut]:
    """The candidate's decision-point searches: every node considered, its scores and why it
    was kept, pruned, rejected or selected."""
    candidate = design.get_candidate(session, scope, candidate_id)
    points = session.scalars(select(DecisionPoint).where(
        DecisionPoint.tenant_id == scope.tenant_id, DecisionPoint.project_id == scope.project_id,
        DecisionPoint.candidate_id == candidate.id).order_by(DecisionPoint.created_at))
    out = []
    for dp in points:
        affected = {e.split(".")[0].strip().lower() for e in dp.scope.get("elements", [])}
        nodes = list(session.scalars(select(SearchNode).where(SearchNode.decision_point_id == dp.id)
                                     .order_by(SearchNode.depth, SearchNode.created_at)))
        root_tables = {t["name"].strip().lower() for n in nodes if n.depth == 0 for t in n.content.get("tables", [])}

        def shown(n: SearchNode) -> list[str]:
            # The tables the decision is about: the affected ones, and any this branch added.
            return [t["name"] for t in n.content.get("tables", [])
                    if t["name"].strip().lower() in affected or t["name"].strip().lower() not in root_tables]

        out.append(DecisionPointOut(
            id=dp.id, trigger=str(dp.trigger), question=dp.question, scope=dp.scope, status=str(dp.status),
            stop_reason=dp.stop_reason, selected_node_id=dp.selected_node_id, decision_id=dp.decision_id,
            usage=dp.usage or {}, created_at=dp.created_at, finished_at=dp.finished_at,
            nodes=[SearchNodeOut(
                id=n.id, parent_id=n.parent_id, depth=n.depth, stage=n.stage, choice=n.choice, rationale=n.rationale,
                evidence_ids=list(n.evidence_ids or []), assumptions=list(n.assumptions or []), status=str(n.status),
                reason=n.reason, evaluation=n.evaluation or {},
                tables=shown(n),
            ) for n in nodes],
        ))
    return out
