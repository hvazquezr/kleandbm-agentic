"""Model, record, changeset, layout, version, and lifecycle endpoints.

Everything here sits under /tenants/{tenant_id}/projects/{project_id}, so ProjectScopeDep
resolves the caller's current project membership before any handler runs.
"""

import uuid
from datetime import datetime
from typing import Any, Generic, TypeVar

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.llm import LLMError
from app.agents.table_suggestion.agent import TableSuggestion, suggest_table
from app.api.deps import ProjectScopeDep, SessionDep
from app.domain import modeling
from app.domain.access import Permission
from app.domain.modeling import Operation
from app.domain.records import (
    AttributeData,
    CheckData,
    DecisionData,
    DefinitionData,
    LinkType,
    ModelState,
    QuestionData,
    RecordType,
    RelationshipData,
    RequirementData,
    TableData,
)

router = APIRouter(prefix="/tenants/{tenant_id}/projects/{project_id}", tags=["modeling"])

T = TypeVar("T", bound=BaseModel)


# --- Schemas --------------------------------------------------------------------------------


class ModelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None


class RecordOut(BaseModel, Generic[T]):
    id: uuid.UUID
    type: RecordType
    code: str | None
    model_id: uuid.UUID | None
    created_seq: int
    seq: int
    data: T


class LinkOut(BaseModel):
    id: uuid.UUID
    from_id: uuid.UUID
    to_id: uuid.UUID
    link_type: LinkType
    created_seq: int


class Position(BaseModel):
    record_id: uuid.UUID
    x: float
    y: float


class StateEventOut(BaseModel):
    state: ModelState
    seq: int | None
    actor_kind: str
    by_name: str | None
    note: str | None
    at: datetime
    # Unconfirmed definitions accepted as exceptions: {definition_id, code, term, seq}.
    exceptions: list[dict[str, Any]] = []


class DefinitionDependencyOut(BaseModel):
    id: uuid.UUID
    code: str | None
    term: str
    provenance: str
    confirmation_status: str
    seq: int
    reasons: list[str]


class StateOut(BaseModel):
    recorded_state: ModelState
    recorded_at_seq: int | None
    effective_state: ModelState
    changed_since_state: bool
    last_event: StateEventOut | None = None


class SnapshotOut(BaseModel):
    model_id: uuid.UUID
    as_of_seq: int
    current_seq: int
    state: StateOut
    tables: list[RecordOut[TableData]]
    attributes: list[RecordOut[AttributeData]]
    relationships: list[RecordOut[RelationshipData]]
    requirements: list[RecordOut[RequirementData]]
    definitions: list[RecordOut[DefinitionData]]
    questions: list[RecordOut[QuestionData]]
    decisions: list[RecordOut[DecisionData]]
    checks: list[RecordOut[CheckData]]
    links: list[LinkOut]
    layout: list[Position]


class ChangesetIn(BaseModel):
    model_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=2000)
    operations: list[Operation] = Field(min_length=1, max_length=500)


class ChangesetOut(BaseModel):
    seq: int
    created: dict[str, uuid.UUID]
    created_links: list[uuid.UUID]


class RecordAnyOut(BaseModel):
    id: uuid.UUID
    type: RecordType
    code: str | None
    model_id: uuid.UUID | None
    created_seq: int
    seq: int
    data: dict[str, Any]


class HistoryOut(BaseModel):
    seq: int
    is_deleted: bool
    data: dict[str, Any]
    actor_kind: str
    actor_id: uuid.UUID
    on_behalf_of: uuid.UUID | None
    reason: str | None
    created_at: datetime


class ImpactOut(BaseModel):
    record: RecordAnyOut
    depth: int
    via: str


class LayoutIn(BaseModel):
    positions: list[Position] = Field(max_length=2000)


class VersionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seq: int
    name: str
    description: str | None
    created_by: uuid.UUID
    created_at: datetime


class StateIn(BaseModel):
    state: ModelState
    note: str | None = Field(default=None, max_length=4000)
    # Accepting only: unconfirmed definitions the model depends on, accepted as assumptions.
    exceptions: list[uuid.UUID] = Field(default_factory=list, max_length=200)


_SNAPSHOT_KEYS = {
    RecordType.TABLE: "tables",
    RecordType.ATTRIBUTE: "attributes",
    RecordType.RELATIONSHIP: "relationships",
    RecordType.REQUIREMENT: "requirements",
    RecordType.DEFINITION: "definitions",
    RecordType.QUESTION: "questions",
    RecordType.DECISION: "decisions",
    RecordType.CHECK: "checks",
}


def _record_dict(view: modeling.RecordView) -> dict[str, Any]:
    return {
        "id": view.id,
        "type": view.type,
        "code": view.code,
        "model_id": view.model_id,
        "created_seq": view.created_seq,
        "seq": view.seq,
        "data": view.data,
    }


def _state_out(state: modeling.ModelStateView) -> StateOut:
    return StateOut(
        recorded_state=state.recorded_state,
        recorded_at_seq=state.recorded_at_seq,
        effective_state=state.effective_state,
        changed_since_state=state.changed_since_state,
        last_event=_event_out(state.last_event) if state.last_event else None,
    )


def _event_out(e: modeling.StateEventView) -> StateEventOut:
    return StateEventOut(state=e.state, seq=e.seq, actor_kind=e.actor_kind.value, by_name=e.by_name,
                         note=e.note, at=e.at, exceptions=e.exceptions)


# --- Routes ---------------------------------------------------------------------------------


@router.get("/models")
def list_models(session: SessionDep, scope: ProjectScopeDep) -> list[ModelOut]:
    return [ModelOut.model_validate(m) for m in modeling.list_models(session, scope)]


@router.post("/models", status_code=status.HTTP_201_CREATED)
def create_model(body: ModelCreate, session: SessionDep, scope: ProjectScopeDep) -> ModelOut:
    return ModelOut.model_validate(
        modeling.create_model(session, scope, body.name, body.description)
    )


@router.get("/models/{model_id}/snapshot")
def get_snapshot(
    model_id: uuid.UUID,
    session: SessionDep,
    scope: ProjectScopeDep,
    as_of: int | None = Query(default=None, description="Changeset seq watermark; default latest"),
) -> SnapshotOut:
    snap = modeling.get_snapshot(session, scope, model_id, as_of)
    body: dict[str, Any] = {
        "model_id": snap.model_id,
        "as_of_seq": snap.as_of_seq,
        "current_seq": snap.current_seq,
        "state": _state_out(snap.state),
        "links": [LinkOut(**vars(link)) for link in snap.links],
        "layout": [Position(record_id=r, x=x, y=y) for r, (x, y) in snap.layout.items()],
    }
    for rtype, key in _SNAPSHOT_KEYS.items():
        body[key] = [_record_dict(v) for v in snap.records[rtype]]
    return SnapshotOut.model_validate(body)


@router.post("/changesets", status_code=status.HTTP_201_CREATED)
def apply_changeset(body: ChangesetIn, session: SessionDep, scope: ProjectScopeDep) -> ChangesetOut:
    result = modeling.apply_changeset(
        session, scope, list(body.operations), model_id=body.model_id, reason=body.reason
    )
    return ChangesetOut(seq=result.seq, created=result.created, created_links=result.created_links)


@router.get("/records/{record_id}")
def get_record(record_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> RecordAnyOut:
    return RecordAnyOut(**_record_dict(modeling.get_record(session, scope, record_id)))


@router.get("/records/{record_id}/history")
def record_history(
    record_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep
) -> list[HistoryOut]:
    return [HistoryOut(**vars(h)) for h in modeling.record_history(session, scope, record_id)]


@router.get("/records/{record_id}/impact")
def record_impact(
    record_id: uuid.UUID,
    session: SessionDep,
    scope: ProjectScopeDep,
    depth: int = Query(default=2, ge=1, le=4),
) -> list[ImpactOut]:
    return [
        ImpactOut(record=RecordAnyOut(**_record_dict(e.record)), depth=e.depth, via=e.via)
        for e in modeling.impact(session, scope, record_id, depth)
    ]


@router.put("/models/{model_id}/layout", status_code=status.HTTP_204_NO_CONTENT)
def set_layout(
    model_id: uuid.UUID, body: LayoutIn, session: SessionDep, scope: ProjectScopeDep
) -> None:
    modeling.set_layout(
        session, scope, model_id, {p.record_id: (p.x, p.y) for p in body.positions}
    )


@router.get("/models/{model_id}/versions")
def list_versions(
    model_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep
) -> list[VersionOut]:
    return [VersionOut.model_validate(v) for v in modeling.list_versions(session, scope, model_id)]


@router.post("/models/{model_id}/versions", status_code=status.HTTP_201_CREATED)
def create_version(
    model_id: uuid.UUID, body: VersionCreate, session: SessionDep, scope: ProjectScopeDep
) -> VersionOut:
    return VersionOut.model_validate(
        modeling.create_version(session, scope, model_id, body.name, body.description)
    )


@router.get("/models/{model_id}/state")
def get_state(model_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> StateOut:
    modeling.get_model(session, scope, model_id)
    return _state_out(modeling.get_state(session, scope, model_id))


@router.get("/models/{model_id}/state/history")
def get_state_history(model_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> list[StateEventOut]:
    return [_event_out(e) for e in modeling.state_history(session, scope, model_id)]


@router.post("/models/{model_id}/state")
def set_state(
    model_id: uuid.UUID, body: StateIn, session: SessionDep, scope: ProjectScopeDep
) -> StateOut:
    return _state_out(modeling.set_state(session, scope, model_id, body.state, body.note, body.exceptions))


class TableSuggestionIn(BaseModel):
    description: str = Field(min_length=1, max_length=2000)


@router.post("/models/{model_id}/table-suggestions")
def create_table_suggestion(
    model_id: uuid.UUID, body: TableSuggestionIn, request: Request, session: SessionDep, scope: ProjectScopeDep
) -> TableSuggestion:
    """Describe with AI: a suggested table and attributes for the New table form, from one model
    call. Nothing is written to the model; the user reviews it and saves it as their own edit.
    503 when no model is configured or the model gives no valid table."""
    scope.require(Permission.PROJECT_EDIT)  # before building the model: 403/404, not 503
    modeling.get_model(session, scope, model_id)
    try:
        chat_model = request.app.state.chat_model_factory("table_suggestion")
        return suggest_table(session, scope, model_id, body.description, chat_model)
    except LLMError as exc:
        detail = " ".join(str(exc).split())  # the full error stays on the run
        detail = detail if len(detail) <= 240 else detail[:239] + "…"
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Describe with AI isn't available: {detail}") from exc


@router.get("/models/{model_id}/definition-dependencies")
def get_definition_dependencies(
    model_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep
) -> list[DefinitionDependencyOut]:
    """Definitions the model depends on and why; accepting it needs each one confirmed or
    accepted as an exception."""
    return [
        DefinitionDependencyOut(
            id=d.definition.id, code=d.definition.code, term=d.definition.data["term"],
            provenance=str(d.definition.data["provenance"]),
            confirmation_status=str(d.definition.data["confirmation_status"]),
            seq=d.definition.seq, reasons=d.reasons,
        )
        for d in modeling.definition_dependencies(session, scope, model_id)
    ]
