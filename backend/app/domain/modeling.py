"""Data models, versioned records, traceability links, and lifecycle state.

All structural writes go through ``apply_changeset``, which appends revisions under a single
new per-project seq. Reads take an optional ``as_of`` seq and return the state at that
watermark (spec §7, "Version history mechanism").
"""

import datetime as dt
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from app.domain.access import ActorKind, NotFoundInScope, Permission, ProjectScope
from app.domain.records import (
    CODE_PREFIXES,
    ConfirmationStatus,
    LINK_RULES,
    MODEL_SCOPED_TYPES,
    PATCHES,
    PAYLOADS,
    REFERENCE_FIELDS,
    LinkType,
    ModelState,
    RecordType,
)
from app.domain.tenancy import InvalidOperation
from app.persistence.modeling import (
    REVISION_TABLES,
    Changeset,
    DataModel,
    ElementLayout,
    ModelStateEvent,
    ModelVersion,
    ProjectSequence,
    Record,
    RecordLink,
)
from app.persistence.models import User


class StaleUpdate(Exception):
    """The record changed after the caller read it (optimistic concurrency, spec §7)."""

    def __init__(self, record_id: uuid.UUID, expected_seq: int | None, current_seq: int) -> None:
        super().__init__(f"record {record_id} is at seq {current_seq}, not {expected_seq}")
        self.record_id = record_id
        self.expected_seq = expected_seq
        self.current_seq = current_seq


# --- Operations -----------------------------------------------------------------------------

# Within one changeset, "$name" refers to the record created by the op with ref "name".
RecordRef = uuid.UUID | Annotated[str, Field(pattern=r"^\$[A-Za-z0-9_.-]{1,64}$")]


class CreateOp(BaseModel):
    op: Literal["create"] = "create"
    type: RecordType
    ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    data: dict[str, Any]


class UpdateOp(BaseModel):
    op: Literal["update"] = "update"
    id: RecordRef
    # The revision seq the caller read. Required unless the record was created in this changeset.
    expected_seq: int | None = None
    data: dict[str, Any]


class DeleteOp(BaseModel):
    op: Literal["delete"] = "delete"
    id: RecordRef
    expected_seq: int | None = None


class LinkOp(BaseModel):
    op: Literal["link"] = "link"
    from_id: RecordRef
    to_id: RecordRef
    link_type: LinkType


class UnlinkOp(BaseModel):
    op: Literal["unlink"] = "unlink"
    link_id: uuid.UUID


Operation = Annotated[
    CreateOp | UpdateOp | DeleteOp | LinkOp | UnlinkOp, Field(discriminator="op")
]


# --- Read models ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordView:
    id: uuid.UUID
    type: RecordType
    code: str | None
    model_id: uuid.UUID | None
    created_seq: int
    seq: int  # seq of the revision shown
    data: dict[str, Any]


@dataclass(frozen=True)
class LinkView:
    id: uuid.UUID
    from_id: uuid.UUID
    to_id: uuid.UUID
    link_type: LinkType
    created_seq: int


@dataclass(frozen=True)
class StateEventView:
    """One lifecycle transition and who made it (spec: acceptance is recorded against a version)."""

    state: ModelState
    seq: int | None
    actor_kind: ActorKind
    # The person: the user who made it, or the user an agent acted for. None if no name is known.
    by_name: str | None
    note: str | None
    at: dt.datetime
    # Unconfirmed definitions the person accepted the model with, as {definition_id, code,
    # term, seq}. Only on Accepted events.
    exceptions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class DefinitionDependency:
    """A definition the model depends on, and why: an element, check or mapped requirement
    uses it, a check of the model reviews it, or it conflicts with one that does."""

    definition: RecordView
    reasons: list[str]


@dataclass(frozen=True)
class ModelStateView:
    recorded_state: ModelState
    recorded_at_seq: int | None
    # Draft whenever the model, or a register entry linked to it, changed after a Ready for
    # Review or Accepted event.
    effective_state: ModelState
    changed_since_state: bool
    # The latest transition, with who made it; None while the model has never left Draft.
    last_event: StateEventView | None = None


@dataclass(frozen=True)
class Snapshot:
    model_id: uuid.UUID
    as_of_seq: int
    current_seq: int
    records: dict[RecordType, list[RecordView]]
    links: list[LinkView]
    layout: dict[uuid.UUID, tuple[float, float]]
    state: ModelStateView


@dataclass(frozen=True)
class ChangesetResult:
    seq: int
    created: dict[str, uuid.UUID]  # ref -> new record id
    created_links: list[uuid.UUID]


@dataclass(frozen=True)
class HistoryEntry:
    seq: int
    is_deleted: bool
    data: dict[str, Any]
    actor_kind: str
    actor_id: uuid.UUID
    on_behalf_of: uuid.UUID | None
    reason: str | None
    created_at: Any


@dataclass(frozen=True)
class ImpactEntry:
    record: RecordView
    depth: int
    via: str  # link type, or "structure" for containment and key references


# --- Models ---------------------------------------------------------------------------------


def create_model(
    session: Session, scope: ProjectScope, name: str, description: str | None = None
) -> DataModel:
    scope.require(Permission.PROJECT_EDIT)
    model = DataModel(
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        name=name,
        description=description,
    )
    session.add(model)
    session.flush()
    return model


def list_models(session: Session, scope: ProjectScope) -> list[DataModel]:
    scope.require(Permission.PROJECT_READ)
    return list(
        session.scalars(
            select(DataModel)
            .where(
                DataModel.tenant_id == scope.tenant_id, DataModel.project_id == scope.project_id
            )
            .order_by(DataModel.name)
        )
    )


def get_model(session: Session, scope: ProjectScope, model_id: uuid.UUID) -> DataModel:
    scope.require(Permission.PROJECT_READ)
    model = session.scalar(
        select(DataModel).where(
            DataModel.tenant_id == scope.tenant_id,
            DataModel.project_id == scope.project_id,
            DataModel.id == model_id,
        )
    )
    if model is None:
        raise NotFoundInScope("model")
    return model


def current_seq(session: Session, scope: ProjectScope) -> int:
    value = session.scalar(
        select(ProjectSequence.last_seq).where(ProjectSequence.project_id == scope.project_id)
    )
    return value or 0


# --- Reading records ------------------------------------------------------------------------


def _latest(
    session: Session,
    scope: ProjectScope,
    rtype: RecordType,
    *,
    as_of: int,
    model_id: uuid.UUID | None = None,
    model_filter: bool = False,
    record_ids: list[uuid.UUID] | None = None,
    include_deleted: bool = False,
) -> list[RecordView]:
    rev_cls = REVISION_TABLES[rtype]
    latest = (
        select(rev_cls)
        .where(
            rev_cls.tenant_id == scope.tenant_id,
            rev_cls.project_id == scope.project_id,
            rev_cls.seq <= as_of,
        )
        .distinct(rev_cls.record_id)
        .order_by(rev_cls.record_id, rev_cls.seq.desc())
    )
    if record_ids is not None:
        latest = latest.where(rev_cls.record_id.in_(record_ids))
    rev = aliased(rev_cls, latest.subquery())
    query = (
        select(Record, rev)
        .join(rev, rev.record_id == Record.id)
        .where(Record.tenant_id == scope.tenant_id, Record.project_id == scope.project_id)
    )
    if model_filter:
        query = query.where(Record.model_id == model_id)
    if not include_deleted:
        query = query.where(rev.is_deleted.is_(False))
    return [_view(record, revision) for record, revision in session.execute(query)]


def _view(record: Record, revision: Any) -> RecordView:
    fields = PAYLOADS[record.record_type].model_fields
    return RecordView(
        id=record.id,
        type=record.record_type,
        code=record.code,
        model_id=record.model_id,
        created_seq=record.created_seq,
        seq=revision.seq,
        data={name: getattr(revision, name) for name in fields},
    )


def _resolve_as_of(session: Session, scope: ProjectScope, as_of: int | None) -> tuple[int, int]:
    latest = current_seq(session, scope)
    if as_of is None:
        return latest, latest
    if as_of < 0 or as_of > latest:
        raise InvalidOperation(f"as_of must be between 0 and {latest}")
    return as_of, latest


def get_snapshot(
    session: Session, scope: ProjectScope, model_id: uuid.UUID, as_of: int | None = None
) -> Snapshot:
    """The model's elements and checks plus the project registers, as of a watermark."""
    get_model(session, scope, model_id)
    watermark, latest = _resolve_as_of(session, scope, as_of)

    records: dict[RecordType, list[RecordView]] = {}
    for rtype in RecordType:
        records[rtype] = _latest(
            session,
            scope,
            rtype,
            as_of=watermark,
            model_id=model_id if rtype in MODEL_SCOPED_TYPES else None,
            model_filter=True,
        )
    visible = {r.id for views in records.values() for r in views}

    links = [
        LinkView(l.id, l.from_id, l.to_id, l.link_type, l.created_seq)
        for l in session.scalars(
            select(RecordLink).where(
                RecordLink.tenant_id == scope.tenant_id,
                RecordLink.project_id == scope.project_id,
                RecordLink.created_seq <= watermark,
                (RecordLink.removed_seq.is_(None)) | (RecordLink.removed_seq > watermark),
            )
        )
        if l.from_id in visible and l.to_id in visible
    ]

    layout = {
        row.record_id: (row.x, row.y)
        for row in session.scalars(
            select(ElementLayout).where(
                ElementLayout.tenant_id == scope.tenant_id,
                ElementLayout.project_id == scope.project_id,
                ElementLayout.model_id == model_id,
            )
        )
        if row.record_id in visible
    }

    return Snapshot(
        model_id=model_id,
        as_of_seq=watermark,
        current_seq=latest,
        records=records,
        links=links,
        layout=layout,
        state=get_state(session, scope, model_id),
    )


def get_record(session: Session, scope: ProjectScope, record_id: uuid.UUID) -> RecordView:
    scope.require(Permission.PROJECT_READ)
    record = _record(session, scope, record_id)
    views = _latest(
        session,
        scope,
        record.record_type,
        as_of=current_seq(session, scope),
        record_ids=[record_id],
    )
    if not views:
        raise NotFoundInScope("record")
    return views[0]


def record_history(session: Session, scope: ProjectScope, record_id: uuid.UUID) -> list[HistoryEntry]:
    scope.require(Permission.PROJECT_READ)
    record = _record(session, scope, record_id)
    rev_cls = REVISION_TABLES[record.record_type]
    fields = PAYLOADS[record.record_type].model_fields
    rows = session.execute(
        select(rev_cls, Changeset)
        .join(
            Changeset,
            (Changeset.tenant_id == rev_cls.tenant_id)
            & (Changeset.project_id == rev_cls.project_id)
            & (Changeset.seq == rev_cls.seq),
        )
        .where(rev_cls.record_id == record_id, rev_cls.project_id == scope.project_id)
        .order_by(rev_cls.seq)
    )
    return [
        HistoryEntry(
            seq=rev.seq,
            is_deleted=rev.is_deleted,
            data={name: getattr(rev, name) for name in fields},
            actor_kind=str(cs.actor_kind),
            actor_id=cs.actor_id,
            on_behalf_of=cs.on_behalf_of,
            reason=cs.reason,
            created_at=cs.created_at,
        )
        for rev, cs in rows
    ]


def _record(session: Session, scope: ProjectScope, record_id: uuid.UUID) -> Record:
    record = session.scalar(
        select(Record).where(
            Record.tenant_id == scope.tenant_id,
            Record.project_id == scope.project_id,
            Record.id == record_id,
        )
    )
    if record is None:
        raise NotFoundInScope("record")
    return record


# --- Writing records ------------------------------------------------------------------------


def apply_changeset(
    session: Session,
    scope: ProjectScope,
    operations: list[CreateOp | UpdateOp | DeleteOp | LinkOp | UnlinkOp],
    *,
    model_id: uuid.UUID | None = None,
    reason: str | None = None,
    settles_decision_point: bool = False,
) -> ChangesetResult:
    """Apply operations atomically as one attributed changeset with a new seq.
    `settles_decision_point` is only for the decision-point resolve action (design.py), the one
    place a decision_point question's status may change."""
    scope.require(Permission.PROJECT_EDIT)
    if not operations:
        raise InvalidOperation("a changeset needs at least one operation")
    if model_id is not None:
        get_model(session, scope, model_id)
    return _ChangesetWriter(session, scope, model_id, reason, settles_decision_point).run(operations)


@dataclass
class _Pending:
    record: Record
    revision: Any
    created_here: bool


@dataclass
class _ChangesetWriter:
    session: Session
    scope: ProjectScope
    model_id: uuid.UUID | None
    reason: str | None
    settles_decision_point: bool = False
    seq: int = 0
    refs: dict[str, uuid.UUID] = field(default_factory=dict)
    pending: dict[uuid.UUID, _Pending] = field(default_factory=dict)
    created_links: list[uuid.UUID] = field(default_factory=list)
    _code_counts: dict[RecordType, int] = field(default_factory=dict)

    def run(self, operations) -> ChangesetResult:
        self.seq = self._next_seq()
        actor = self.scope.actor
        self.session.add(
            Changeset(
                tenant_id=self.scope.tenant_id,
                project_id=self.scope.project_id,
                seq=self.seq,
                model_id=self.model_id,
                actor_kind=actor.kind,
                actor_id=actor.id,
                on_behalf_of=actor.on_behalf_of,
                reason=self.reason,
            )
        )
        self.session.flush()
        for index, op in enumerate(operations):
            try:
                self._apply(op)
            except ValidationError as exc:
                raise InvalidOperation(f"operation {index}: {exc.errors(include_url=False)}") from exc
            except InvalidOperation as exc:
                raise InvalidOperation(f"operation {index}: {exc}") from exc
        self.session.flush()
        return ChangesetResult(seq=self.seq, created=dict(self.refs), created_links=self.created_links)

    def _next_seq(self) -> int:
        # Upsert + RETURNING takes a row lock on the project's sequence, which serializes
        # concurrent changesets in a project and gives gap-free ordering.
        stmt = (
            pg_insert(ProjectSequence)
            .values(tenant_id=self.scope.tenant_id, project_id=self.scope.project_id, last_seq=1)
            .on_conflict_do_update(
                index_elements=[ProjectSequence.project_id],
                set_={"last_seq": ProjectSequence.last_seq + 1},
            )
            .returning(ProjectSequence.last_seq)
        )
        return self.session.execute(stmt).scalar_one()

    # -- dispatch

    def _apply(self, op) -> None:
        match op:
            case CreateOp():
                self._create(op)
            case UpdateOp():
                self._update(op)
            case DeleteOp():
                self._delete(op.id, op.expected_seq)
            case LinkOp():
                self._link(op)
            case UnlinkOp():
                self._unlink(op)

    # -- helpers

    def _resolve(self, value: uuid.UUID | str) -> uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        if isinstance(value, str) and value.startswith("$"):
            try:
                return self.refs[value[1:]]
            except KeyError:
                raise InvalidOperation(f"unknown reference {value}") from None
        return uuid.UUID(str(value))

    def _current(self, record_id: uuid.UUID) -> tuple[Record, dict[str, Any], int, bool]:
        """(record, data, seq, created_here) for a live record, including pending changes."""
        if record_id in self.pending:
            p = self.pending[record_id]
            if p.revision.is_deleted:
                raise InvalidOperation(f"record {record_id} was deleted in this changeset")
            fields = PAYLOADS[p.record.record_type].model_fields
            data = {name: getattr(p.revision, name) for name in fields}
            return p.record, data, self.seq, p.created_here
        try:
            record = _record(self.session, self.scope, record_id)
        except NotFoundInScope:
            raise InvalidOperation(f"unknown record {record_id}") from None
        views = _latest(
            self.session, self.scope, record.record_type, as_of=self.seq - 1, record_ids=[record_id]
        )
        if not views:
            raise InvalidOperation(f"record {record_id} is deleted")
        return record, views[0].data, views[0].seq, False

    def _check_references(self, rtype: RecordType, data: dict[str, Any]) -> None:
        for field_name, target_type in REFERENCE_FIELDS.get(rtype, {}).items():
            target_id = data[field_name]
            target, target_data, _, _ = self._current(target_id)
            if target.record_type is not target_type:
                raise InvalidOperation(f"{field_name} must reference a {target_type}")
            if target.model_id != self.model_id:
                raise InvalidOperation(f"{field_name} must reference a record in the same model")
        if rtype is RecordType.RELATIONSHIP:
            for side in ("from", "to"):
                _, attr, _, _ = self._current(data[f"{side}_attribute_id"])
                if attr["table_id"] != data[f"{side}_table_id"]:
                    raise InvalidOperation(f"{side}_attribute_id does not belong to {side}_table_id")

    def _resolve_reference_fields(self, rtype: RecordType, data: dict[str, Any]) -> dict[str, Any]:
        out = dict(data)
        for field_name in REFERENCE_FIELDS.get(rtype, {}):
            if out.get(field_name) is not None:
                out[field_name] = self._resolve(out[field_name])
        return out

    def _require_model_for(self, rtype: RecordType) -> uuid.UUID | None:
        if rtype in MODEL_SCOPED_TYPES:
            if self.model_id is None:
                raise InvalidOperation(f"{rtype} records require a model_id on the changeset")
            return self.model_id
        return None

    def _next_code(self, rtype: RecordType) -> str | None:
        prefix = CODE_PREFIXES.get(rtype)
        if prefix is None:
            return None
        if rtype not in self._code_counts:
            self._code_counts[rtype] = self.session.scalar(
                select(func.count())
                .select_from(Record)
                .where(Record.project_id == self.scope.project_id, Record.record_type == rtype)
            )
        self._code_counts[rtype] += 1
        return f"{prefix}-{self._code_counts[rtype]:03d}"

    def _write_revision(
        self, record: Record, data: dict[str, Any], *, deleted: bool, created_here: bool
    ) -> None:
        if record.id in self.pending:
            revision = self.pending[record.id].revision
            for name, value in data.items():
                setattr(revision, name, value)
            revision.is_deleted = deleted
            return
        rev_cls = REVISION_TABLES[record.record_type]
        revision = rev_cls(
            tenant_id=self.scope.tenant_id,
            project_id=self.scope.project_id,
            record_id=record.id,
            seq=self.seq,
            is_deleted=deleted,
            **data,
        )
        self.session.add(revision)
        self.pending[record.id] = _Pending(record, revision, created_here)

    # -- operations

    # Status fields that record a human decision about meaning or design (spec §2, §7).
    _AUTHORITY_FIELDS = {
        RecordType.REQUIREMENT: "confirmation_status",
        RecordType.DEFINITION: "confirmation_status",
        RecordType.DECISION: "status",
    }
    _HUMAN_ONLY_VALUES = {"confirmed", "rejected"}
    # Technical assessments an agent may still revise on a confirmed entry: confirmation covers
    # the business meaning, while feasibility depends on source evidence the Architect gathers.
    _AGENT_ASSESSMENT_FIELDS = {RecordType.REQUIREMENT: {"feasibility_status", "feasibility_notes"}}

    def _guard_agent_authority(
        self, rtype: RecordType, current: dict[str, Any] | None, data: dict[str, Any]
    ) -> None:
        """Agents may propose, never decide: they can't confirm or reject meanings or decisions,
        and they can't alter an entry a person already confirmed. They raise a question instead."""
        if self.scope.actor.kind is not ActorKind.AGENT or rtype not in self._AUTHORITY_FIELDS:
            return
        name = self._AUTHORITY_FIELDS[rtype]
        new = str(data.get(name))
        old = str(current.get(name)) if current else None
        if new in self._HUMAN_ONLY_VALUES and new != old:
            raise InvalidOperation(f"agents can't set {name} to {new}; only a person can")
        exempt = self._AGENT_ASSESSMENT_FIELDS.get(rtype, set())
        if old == "confirmed" and any(current.get(k) != data.get(k) for k in set(current) | set(data) if k not in exempt):
            raise InvalidOperation("this entry was confirmed by a person; raise a question instead of changing it")

    # Business meanings (requirements, definitions) are confirmed or rejected only by people with
    # approval rights (spec §7); design decisions are technical choices, so editors may settle them.
    _APPROVAL_TYPES = {RecordType.REQUIREMENT, RecordType.DEFINITION}

    def _guard_person_authority(self, rtype: RecordType, current: dict[str, Any] | None, data: dict[str, Any]) -> None:
        if self.scope.actor.kind is not ActorKind.USER or rtype not in self._APPROVAL_TYPES:
            return
        name = self._AUTHORITY_FIELDS[rtype]
        new, old = str(data.get(name)), (str(current.get(name)) if current else None)
        if new != old and (new in self._HUMAN_ONLY_VALUES or old in self._HUMAN_ONLY_VALUES):
            self.scope.require(Permission.PROJECT_APPROVE)

    def _create(self, op: CreateOp) -> None:
        if op.ref is not None and op.ref in self.refs:
            raise InvalidOperation(f"duplicate ref {op.ref}")
        model_id = self._require_model_for(op.type)
        raw = self._resolve_reference_fields(op.type, op.data)
        payload = PAYLOADS[op.type].model_validate(raw)
        data = payload.model_dump(mode="python")
        self._guard_agent_authority(op.type, None, data)
        self._guard_person_authority(op.type, None, data)
        self._check_references(op.type, data)
        record = Record(
            tenant_id=self.scope.tenant_id,
            project_id=self.scope.project_id,
            model_id=model_id,
            record_type=op.type,
            code=self._next_code(op.type),
            created_seq=self.seq,
        )
        self.session.add(record)
        self.session.flush()
        self._write_revision(record, data, deleted=False, created_here=True)
        if op.ref is not None:
            self.refs[op.ref] = record.id

    def _update(self, op: UpdateOp) -> None:
        record_id = self._resolve(op.id)
        record, current, current_seq_, created_here = self._current(record_id)
        self._check_expected(record_id, op.expected_seq, current_seq_, created_here)
        if record.model_id is not None and record.model_id != self.model_id:
            raise InvalidOperation("record belongs to a different model than this changeset")
        patch = PATCHES[record.record_type].model_validate(
            self._resolve_reference_fields(record.record_type, op.data)
        )
        merged = {**current, **patch.model_dump(mode="python", exclude_unset=True)}
        data = PAYLOADS[record.record_type].model_validate(merged).model_dump(mode="python")
        self._guard_agent_authority(record.record_type, current, data)
        self._guard_person_authority(record.record_type, current, data)
        self._guard_decision_point(record, current, data, created_here)
        self._check_references(record.record_type, data)
        self._write_revision(record, data, deleted=False, created_here=created_here)

    def _guard_decision_point(self, record: Record, current: dict[str, Any], data: dict[str, Any] | None,
                              created_here: bool) -> None:
        """A decision_point question is settled only by accepting or rejecting the search's design
        (`design.resolve_decision_point`), which also records the decision and clears the readiness
        blocker. Answering, resolving or deleting it any other way would leave the blocker in place
        without saying why, so it's refused, for people and agents alike."""
        if (created_here or self.settles_decision_point or record.record_type is not RecordType.QUESTION
                or current.get("kind") != "decision_point"):
            return
        if data is None or str(data.get("status")) != str(current.get("status")):
            raise InvalidOperation(
                "this question is about a design search that didn't select a design; settle it on the Review tab "
                "by accepting or rejecting that design (which records the decision and clears its blocker)")

    def _check_expected(
        self, record_id: uuid.UUID, expected: int | None, current: int, created_here: bool
    ) -> None:
        if created_here:
            return
        if expected is None:
            raise InvalidOperation("expected_seq is required when changing an existing record")
        if expected != current:
            raise StaleUpdate(record_id, expected, current)

    def _delete(self, ref: uuid.UUID | str, expected_seq: int | None, *, cascade: bool = False) -> None:
        record_id = self._resolve(ref)
        record, current, current_seq_, created_here = self._current(record_id)
        if not cascade:
            self._check_expected(record_id, expected_seq, current_seq_, created_here)
            if record.model_id is not None and record.model_id != self.model_id:
                raise InvalidOperation("record belongs to a different model than this changeset")
        self._guard_decision_point(record, current, None, created_here)
        self._write_revision(record, current, deleted=True, created_here=created_here)
        self._remove_links_touching(record_id)
        for dependent_id in self._structural_dependents(record):
            already = self.pending.get(dependent_id)
            if already is not None and already.revision.is_deleted:
                continue  # removed earlier in this cascade
            self._delete(dependent_id, None, cascade=True)
        self.session.execute(
            delete(ElementLayout).where(
                ElementLayout.project_id == self.scope.project_id,
                ElementLayout.record_id == record_id,
            )
        )

    def _structural_dependents(self, record: Record) -> list[uuid.UUID]:
        """Attributes of a deleted table, and relationships using a deleted table or attribute."""
        if record.record_type not in (RecordType.TABLE, RecordType.ATTRIBUTE):
            return []
        dependents: list[uuid.UUID] = []
        candidates = [RecordType.RELATIONSHIP]
        if record.record_type is RecordType.TABLE:
            candidates.insert(0, RecordType.ATTRIBUTE)
        for rtype in candidates:
            for view in self._live_in_model(rtype):
                refs = {view.data[f] for f in REFERENCE_FIELDS[rtype]}
                if record.id in refs:
                    dependents.append(view.id)
        return dependents

    def _live_in_model(self, rtype: RecordType) -> list[RecordView]:
        stored = {
            v.id: v
            for v in _latest(
                self.session,
                self.scope,
                rtype,
                as_of=self.seq - 1,
                model_id=self.model_id,
                model_filter=True,
            )
        }
        for record_id, p in self.pending.items():
            if p.record.record_type is not rtype:
                continue
            if p.revision.is_deleted:
                stored.pop(record_id, None)
            else:
                fields = PAYLOADS[rtype].model_fields
                stored[record_id] = RecordView(
                    id=record_id,
                    type=rtype,
                    code=p.record.code,
                    model_id=p.record.model_id,
                    created_seq=p.record.created_seq,
                    seq=self.seq,
                    data={name: getattr(p.revision, name) for name in fields},
                )
        return list(stored.values())

    def _remove_links_touching(self, record_id: uuid.UUID) -> None:
        for link in self.session.scalars(
            select(RecordLink).where(
                RecordLink.project_id == self.scope.project_id,
                RecordLink.removed_seq.is_(None),
                (RecordLink.from_id == record_id) | (RecordLink.to_id == record_id),
            )
        ):
            link.removed_seq = self.seq

    def _link(self, op: LinkOp) -> None:
        from_id, to_id = self._resolve(op.from_id), self._resolve(op.to_id)
        if from_id == to_id:
            raise InvalidOperation("a record cannot link to itself")
        source, _, _, _ = self._current(from_id)
        target, _, _, _ = self._current(to_id)
        allowed_from, allowed_to = LINK_RULES[op.link_type]
        if source.record_type not in allowed_from or target.record_type not in allowed_to:
            raise InvalidOperation(
                f"{op.link_type} cannot link {source.record_type} to {target.record_type}"
            )
        exists = self.session.scalar(
            select(func.count())
            .select_from(RecordLink)
            .where(
                RecordLink.from_id == from_id,
                RecordLink.to_id == to_id,
                RecordLink.link_type == op.link_type,
                RecordLink.removed_seq.is_(None),
            )
        )
        if exists:
            raise InvalidOperation("link already exists")
        link = RecordLink(
            tenant_id=self.scope.tenant_id,
            project_id=self.scope.project_id,
            from_id=from_id,
            to_id=to_id,
            link_type=op.link_type,
            created_seq=self.seq,
        )
        self.session.add(link)
        self.session.flush()
        self.created_links.append(link.id)

    def _unlink(self, op: UnlinkOp) -> None:
        link = self.session.scalar(
            select(RecordLink).where(
                RecordLink.tenant_id == self.scope.tenant_id,
                RecordLink.project_id == self.scope.project_id,
                RecordLink.id == op.link_id,
                RecordLink.removed_seq.is_(None),
            )
        )
        if link is None:
            raise InvalidOperation(f"no live link {op.link_id}")
        link.removed_seq = self.seq


def list_live_links(session: Session, scope: ProjectScope) -> list[RecordLink]:
    """Links that are currently in effect anywhere in the project."""
    scope.require(Permission.PROJECT_READ)
    return list(
        session.scalars(
            select(RecordLink).where(
                RecordLink.tenant_id == scope.tenant_id,
                RecordLink.project_id == scope.project_id,
                RecordLink.removed_seq.is_(None),
            )
        )
    )


# --- Impact analysis ------------------------------------------------------------------------


def impact(
    session: Session, scope: ProjectScope, record_id: uuid.UUID, max_depth: int = 2
) -> list[ImpactEntry]:
    """Records reachable from ``record_id`` through live links and structural references.

    Links are followed in both directions, because a change on either end can invalidate the
    other (spec §7: impact analysis for definitions, requirements, and elements).
    """
    scope.require(Permission.PROJECT_READ)
    _record(session, scope, record_id)
    seq = current_seq(session, scope)
    views: dict[uuid.UUID, RecordView] = {}
    for rtype in RecordType:
        for view in _latest(session, scope, rtype, as_of=seq):
            views[view.id] = view
    if record_id not in views:
        raise NotFoundInScope("record")

    edges: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = {}

    def connect(a: uuid.UUID, b: uuid.UUID, via: str) -> None:
        edges.setdefault(a, []).append((b, via))
        edges.setdefault(b, []).append((a, via))

    for link in session.scalars(
        select(RecordLink).where(
            RecordLink.tenant_id == scope.tenant_id,
            RecordLink.project_id == scope.project_id,
            RecordLink.removed_seq.is_(None),
        )
    ):
        if link.from_id in views and link.to_id in views:
            connect(link.from_id, link.to_id, str(link.link_type))
    for view in views.values():
        for field_name in REFERENCE_FIELDS.get(view.type, {}):
            target = view.data[field_name]
            if target in views:
                connect(view.id, target, "structure")

    found: dict[uuid.UUID, ImpactEntry] = {}
    queue = deque([(record_id, 0)])
    seen = {record_id}
    while queue:
        current, depth = queue.popleft()
        if depth == max_depth:
            continue
        for neighbour, via in edges.get(current, []):
            if neighbour in seen:
                continue
            seen.add(neighbour)
            found[neighbour] = ImpactEntry(record=views[neighbour], depth=depth + 1, via=via)
            queue.append((neighbour, depth + 1))
    return sorted(found.values(), key=lambda e: (e.depth, str(e.record.type), e.record.code or ""))


# --- Layout ---------------------------------------------------------------------------------


def set_layout(
    session: Session,
    scope: ProjectScope,
    model_id: uuid.UUID,
    positions: dict[uuid.UUID, tuple[float, float]],
) -> None:
    """Store canvas positions. Cosmetic: no changeset and no revision (spec §4)."""
    scope.require(Permission.PROJECT_EDIT)
    get_model(session, scope, model_id)
    if not positions:
        return
    seq = current_seq(session, scope)
    live = {
        v.id
        for rtype in (RecordType.TABLE,)
        for v in _latest(session, scope, rtype, as_of=seq, model_id=model_id, model_filter=True)
    }
    unknown = set(positions) - live
    if unknown:
        raise InvalidOperation(f"not live tables in this model: {sorted(map(str, unknown))}")
    for record_id, (x, y) in positions.items():
        stmt = (
            pg_insert(ElementLayout)
            .values(
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                model_id=model_id,
                record_id=record_id,
                x=x,
                y=y,
            )
            .on_conflict_do_update(
                index_elements=[ElementLayout.record_id], set_={"x": x, "y": y}
            )
        )
        session.execute(stmt)


# --- Named versions -------------------------------------------------------------------------


def create_version(
    session: Session,
    scope: ProjectScope,
    model_id: uuid.UUID,
    name: str,
    description: str | None = None,
) -> ModelVersion:
    scope.require(Permission.PROJECT_EDIT)
    get_model(session, scope, model_id)
    seq = current_seq(session, scope)
    if seq == 0:
        raise InvalidOperation("there are no changes to version yet")
    version = ModelVersion(
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        model_id=model_id,
        seq=seq,
        name=name,
        description=description,
        created_by=scope.actor.on_behalf_of or scope.actor.id,
    )
    session.add(version)
    session.flush()
    return version


def list_versions(session: Session, scope: ProjectScope, model_id: uuid.UUID) -> list[ModelVersion]:
    get_model(session, scope, model_id)
    return list(
        session.scalars(
            select(ModelVersion)
            .where(
                ModelVersion.project_id == scope.project_id, ModelVersion.model_id == model_id
            )
            .order_by(ModelVersion.seq.desc(), ModelVersion.created_at.desc())
        )
    )


# --- Lifecycle state ------------------------------------------------------------------------


def set_state(
    session: Session,
    scope: ProjectScope,
    model_id: uuid.UUID,
    state: ModelState,
    note: str | None = None,
    exceptions: Iterable[uuid.UUID] = (),
) -> ModelStateView:
    """Record a lifecycle transition against the current watermark.

    Acceptance needs approval authority, which agents never hold, and every definition the
    model depends on must be confirmed by a person or named in ``exceptions`` (accepted as an
    assumption, with a note saying why). A rejected definition the model still uses can't be
    excepted. Ready for Review doesn't need confirmed definitions; unconfirmed meanings stay
    listed for the reviewer (spec §7).
    """
    get_model(session, scope, model_id)
    exceptions = list(dict.fromkeys(exceptions))
    if state is ModelState.ACCEPTED:
        scope.require(Permission.PROJECT_APPROVE)
    else:
        scope.require(Permission.PROJECT_EDIT)
        if exceptions:
            raise InvalidOperation("definition exceptions apply only when accepting a model")
    seq = current_seq(session, scope) or None
    if state is not ModelState.DRAFT and seq is None:
        raise InvalidOperation(f"an empty model cannot be {state}")
    accepted: list[dict[str, Any]] = []
    if state is ModelState.ACCEPTED:
        accepted = _acceptance_exceptions(session, scope, model_id, exceptions, note)
    actor = scope.actor
    session.add(
        ModelStateEvent(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            model_id=model_id,
            seq=seq,
            state=state,
            actor_kind=actor.kind,
            actor_id=actor.id,
            on_behalf_of=actor.on_behalf_of,
            note=note,
            accepted_exceptions=accepted,
        )
    )
    session.flush()
    return get_state(session, scope, model_id)


def _label(d: RecordView) -> str:
    return f"{d.code} {d.data['term']}" if d.code else d.data["term"]


def _acceptance_exceptions(
    session: Session, scope: ProjectScope, model_id: uuid.UUID, exceptions: list[uuid.UUID], note: str | None,
) -> list[dict[str, Any]]:
    open_: dict[uuid.UUID, RecordView] = {}
    rejected: list[str] = []
    for dep in definition_dependencies(session, scope, model_id):
        status = dep.definition.data["confirmation_status"]
        if status == ConfirmationStatus.REJECTED:
            rejected.append(_label(dep.definition))
        elif status != ConfirmationStatus.CONFIRMED:
            open_[dep.definition.id] = dep.definition
    if rejected:
        raise InvalidOperation("the model still uses rejected definitions: " + ", ".join(rejected)
                               + "; revise the model or the requirements before accepting it")
    unknown = [str(i) for i in exceptions if i not in open_]
    if unknown:
        raise InvalidOperation("exceptions must be unconfirmed definitions the model depends on: "
                               + ", ".join(unknown))
    missing = [_label(d) for i, d in open_.items() if i not in exceptions]
    if missing:
        raise InvalidOperation("the model depends on unconfirmed definitions: " + ", ".join(missing)
                               + "; confirm them or accept them as exceptions")
    if exceptions and not (note and note.strip()):
        raise InvalidOperation("accepting with definition exceptions needs a note saying why")
    return [{"definition_id": str(i), "code": open_[i].code, "term": open_[i].data["term"],
             "seq": open_[i].seq} for i in exceptions]


def definition_dependencies(session: Session, scope: ProjectScope, model_id: uuid.UUID) -> list[DefinitionDependency]:
    """The definitions the model depends on now: those its elements and checks use, those its
    mapped requirements use, those its checks review, and those that conflict with any of them
    (unless the conflicting one is rejected). Live links and records only."""
    scope.require(Permission.PROJECT_READ)
    get_model(session, scope, model_id)
    seq = current_seq(session, scope)
    elements: dict[uuid.UUID, str] = {}
    tables = {v.id: v.data["name"] for v in _latest(
        session, scope, RecordType.TABLE, as_of=seq, model_id=model_id, model_filter=True)}
    elements.update(tables)
    for v in _latest(session, scope, RecordType.ATTRIBUTE, as_of=seq, model_id=model_id, model_filter=True):
        elements[v.id] = f"{tables.get(v.data['table_id'], '?')}.{v.data['name']}"
    for v in _latest(session, scope, RecordType.RELATIONSHIP, as_of=seq, model_id=model_id, model_filter=True):
        elements[v.id] = "a relationship"
    checks = {v.id: v.data.get("issue_key") or v.code or "a check" for v in _latest(
        session, scope, RecordType.CHECK, as_of=seq, model_id=model_id, model_filter=True)}
    requirements = {v.id: v for v in _latest(session, scope, RecordType.REQUIREMENT, as_of=seq)}
    definitions = {v.id: v for v in _latest(session, scope, RecordType.DEFINITION, as_of=seq)}
    links = list_live_links(session, scope)
    users: dict[uuid.UUID, str] = {i: f"used by {n}" for i, n in elements.items()}
    users |= {i: f"used by check {n}" for i, n in checks.items()}
    for l in links:
        if l.link_type is LinkType.MAPS_TO and l.to_id in elements and l.from_id in requirements:
            r = requirements[l.from_id]
            users[r.id] = f"used by requirement {r.code or r.data['statement'][:60]}"
    reasons: dict[uuid.UUID, list[str]] = {}

    def add(def_id: uuid.UUID, reason: str) -> None:
        if def_id in definitions and reason not in reasons.setdefault(def_id, []):
            reasons[def_id].append(reason)

    for l in links:
        if l.link_type is LinkType.USES_DEFINITION and l.from_id in users:
            add(l.to_id, users[l.from_id])
        elif l.link_type is LinkType.CHECKS and l.from_id in checks:
            add(l.to_id, f"reviewed by check {checks[l.from_id]}")
    used = {i: definitions[i] for i in reasons}
    for l in links:
        if l.link_type is not LinkType.CONFLICTS_WITH:
            continue
        for a, b in ((l.from_id, l.to_id), (l.to_id, l.from_id)):
            if a in used and b in definitions and b not in used \
                    and definitions[b].data["confirmation_status"] != ConfirmationStatus.REJECTED:
                add(b, f"conflicts with {_label(used[a])}")
    return sorted((DefinitionDependency(definitions[i], r) for i, r in reasons.items()),
                  key=lambda d: (d.definition.code or "", d.definition.data["term"]))


def get_state(session: Session, scope: ProjectScope, model_id: uuid.UUID) -> ModelStateView:
    event = session.scalar(
        select(ModelStateEvent)
        .where(
            ModelStateEvent.project_id == scope.project_id,
            ModelStateEvent.model_id == model_id,
        )
        .order_by(ModelStateEvent.created_at.desc(), ModelStateEvent.id.desc())
        .limit(1)
    )
    if event is None:
        return ModelStateView(ModelState.DRAFT, None, ModelState.DRAFT, False)
    changed = _changed_since(session, scope, model_id, event.seq or 0)
    effective = ModelState.DRAFT if changed else event.state
    return ModelStateView(event.state, event.seq, effective, changed, _event_view(session, event))


_REGISTER_TYPES = (RecordType.REQUIREMENT, RecordType.DEFINITION, RecordType.QUESTION, RecordType.DECISION)
# From a register entry linked to the model, these links reach further entries that shape it:
# definitions it uses or conflicts with, and questions, decisions and alternatives about it.
_OUTWARD = (LinkType.USES_DEFINITION, LinkType.CONFLICTS_WITH)
_INWARD = (LinkType.CONCERNS, LinkType.AFFECTS, LinkType.ALTERNATIVE_FOR, LinkType.CONFLICTS_WITH)


def _changed_since(session: Session, scope: ProjectScope, model_id: uuid.UUID, seq: int) -> bool:
    """Whether the model changed after a lifecycle event at ``seq``: a changeset on the model
    itself, or a change to a register entry linked to it (or to those links). Register changes
    that concern only other models in the project don't count."""
    own = session.scalar(
        select(func.count()).select_from(Changeset).where(
            Changeset.project_id == scope.project_id, Changeset.seq > seq, Changeset.model_id == model_id))
    if own:
        return True
    return registers_changed_since(session, scope, model_id, seq)


def registers_changed_since(
    session: Session, scope: ProjectScope, model_id: uuid.UUID, seq: int, *,
    also: Iterable[uuid.UUID] = (), ignore_seqs: Iterable[int] = (),
) -> bool:
    """Whether a register entry that shapes the model changed after ``seq``, or a link to one
    was added or removed. The entries are those linked to the model's elements and checks, plus
    ``also`` (entries a design candidate refers to before its links exist), and what shapes
    them transitively through register entries. Changesets in ``ignore_seqs`` don't count."""
    ignore = set(ignore_seqs)
    model_records = set(session.scalars(
        select(Record.id).where(Record.project_id == scope.project_id, Record.model_id == model_id)))
    # Links in effect at the watermark or added since; a link removed since is a change in itself.
    links = list(session.scalars(select(RecordLink).where(
        RecordLink.project_id == scope.project_id,
        (RecordLink.removed_seq.is_(None)) | (RecordLink.removed_seq > seq))))
    registers = set(session.scalars(select(Record.id).where(
        Record.project_id == scope.project_id, Record.model_id.is_(None),
        Record.record_type.in_(_REGISTER_TYPES))))
    # Entries linked directly to the model's elements and checks, in either direction.
    linked = {l.to_id if l.from_id in model_records else l.from_id for l in links
              if (l.from_id in model_records) != (l.to_id in model_records)} & registers
    linked |= set(also) & registers
    if not model_records and not linked:
        return False
    # Then what shapes those entries, transitively through register entries only.
    frontier = set(linked)
    while frontier:
        found = set()
        for l in links:
            if l.from_id in frontier and l.link_type in _OUTWARD:
                found.add(l.to_id)
            if l.to_id in frontier and l.link_type in _INWARD:
                found.add(l.from_id)
        frontier = (found & registers) - linked
        linked |= frontier
    def relevant(l: RecordLink) -> bool:
        if l.from_id in model_records or l.to_id in model_records:
            return True
        # Between register entries, one of them already linked (not another model's element).
        return l.from_id in registers and l.to_id in registers and (l.from_id in linked or l.to_id in linked)

    def counts(link_seq: int | None) -> bool:
        return link_seq is not None and link_seq > seq and link_seq not in ignore

    if any((counts(l.created_seq) or counts(l.removed_seq)) and relevant(l) for l in links):
        return True
    if not linked:
        return False
    for rtype in _REGISTER_TYPES:
        rev = REVISION_TABLES[rtype]
        query = select(func.count()).select_from(rev).where(
            rev.project_id == scope.project_id, rev.seq > seq, rev.record_id.in_(linked))
        if ignore:
            query = query.where(rev.seq.not_in(ignore))
        if session.scalar(query):
            return True
    return False


def state_history(session: Session, scope: ProjectScope, model_id: uuid.UUID) -> list[StateEventView]:
    """Every lifecycle transition of the model, newest first."""
    get_model(session, scope, model_id)
    events = session.scalars(
        select(ModelStateEvent)
        .where(ModelStateEvent.project_id == scope.project_id, ModelStateEvent.model_id == model_id)
        .order_by(ModelStateEvent.created_at.desc(), ModelStateEvent.id.desc())
    )
    return [_event_view(session, e) for e in events]


def _event_view(session: Session, event: ModelStateEvent) -> StateEventView:
    kind = ActorKind(event.actor_kind)
    person_id = event.on_behalf_of if kind is ActorKind.AGENT else event.actor_id
    user = session.get(User, person_id) if person_id else None
    return StateEventView(
        state=ModelState(event.state),
        seq=event.seq,
        actor_kind=kind,
        by_name=(user.display_name or user.email) if user else None,
        note=event.note,
        at=event.created_at,
        exceptions=list(event.accepted_exceptions or []),
    )
