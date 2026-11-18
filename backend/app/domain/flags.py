"""Flags on untrusted content: a document passage or a source table or column whose text tried
to instruct an agent. Agents record them as they find them (`flag_untrusted_content`); people
see them on the Review tab, the documents and the sources, and mark them reviewed.

A flag is a durable note, never a gate: nothing is blocked, and the content itself is unchanged.
Its quote is the planted text and stays untrusted data wherever it's shown.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.access import AccessDenied, ActorKind, NotFoundInScope, Permission, ProjectScope
from app.domain.tenancy import InvalidOperation
from app.persistence.conversations import ContentFlag
from app.persistence.documents import DocumentVersion
from app.persistence.sources import SourceConnection

TARGETS = ("document", "source")


def record_flag(
    session: Session, scope: ProjectScope, *, run_id: uuid.UUID, agent: str, target_kind: str, quote: str,
    reason: str, document_version_id: uuid.UUID | None = None, location: dict[str, Any] | None = None,
    source_id: uuid.UUID | None = None, source_table: str | None = None, source_column: str | None = None,
) -> tuple[ContentFlag, bool]:
    """Record a flag, or return the open one already on the same spot. Returns (flag, created)."""
    scope.require(Permission.PROJECT_EDIT)
    quote, reason = (quote or "").strip()[:300], (reason or "").strip()[:1000]
    if target_kind not in TARGETS:
        raise InvalidOperation(f"target must be one of {', '.join(TARGETS)}")
    if not quote or not reason:
        raise InvalidOperation("quote the instruction and say why it's an attempt to instruct an agent")
    if target_kind == "document":
        version = document_version_id and session.scalar(select(DocumentVersion).where(
            DocumentVersion.tenant_id == scope.tenant_id, DocumentVersion.project_id == scope.project_id,
            DocumentVersion.id == document_version_id))
        if not version:
            raise NotFoundInScope("document version")
        source_id = source_table = source_column = None
    else:
        source = source_id and session.scalar(select(SourceConnection).where(
            SourceConnection.tenant_id == scope.tenant_id, SourceConnection.project_id == scope.project_id,
            SourceConnection.id == source_id))
        if not source:
            raise NotFoundInScope("source")
        if not (source_table or "").strip():
            raise InvalidOperation("name the source table (and column, if it's a column comment or value)")
        document_version_id, location = None, None
    existing = session.scalar(select(ContentFlag).where(
        ContentFlag.tenant_id == scope.tenant_id, ContentFlag.project_id == scope.project_id,
        ContentFlag.reviewed_at.is_(None), ContentFlag.target_kind == target_kind,
        ContentFlag.document_version_id.is_not_distinct_from(document_version_id),
        ContentFlag.source_id.is_not_distinct_from(source_id),
        ContentFlag.source_table.is_not_distinct_from((source_table or "").strip() or None),
        ContentFlag.source_column.is_not_distinct_from((source_column or "").strip() or None),
        ContentFlag.quote == quote))
    if existing:
        return existing, False
    flag = ContentFlag(
        tenant_id=scope.tenant_id, project_id=scope.project_id, run_id=run_id, agent=agent, target_kind=target_kind,
        document_version_id=document_version_id, location=location, source_id=source_id,
        source_table=(source_table or "").strip() or None, source_column=(source_column or "").strip() or None,
        quote=quote, reason=reason)
    session.add(flag)
    session.flush()
    return flag, True


def list_flags(session: Session, scope: ProjectScope, *, include_reviewed: bool = False) -> list[ContentFlag]:
    scope.require(Permission.PROJECT_READ)
    q = select(ContentFlag).where(ContentFlag.tenant_id == scope.tenant_id, ContentFlag.project_id == scope.project_id)
    if not include_reviewed:
        q = q.where(ContentFlag.reviewed_at.is_(None))
    return list(session.scalars(q.order_by(ContentFlag.created_at.desc())))


def review_flag(session: Session, scope: ProjectScope, flag_id: uuid.UUID, note: str | None) -> ContentFlag:
    """A person marks a flag reviewed (the content was looked at). Agents can't."""
    scope.require(Permission.PROJECT_EDIT)
    if scope.actor.kind is not ActorKind.USER:
        raise AccessDenied(Permission.PROJECT_EDIT)
    flag = session.scalar(select(ContentFlag).where(
        ContentFlag.tenant_id == scope.tenant_id, ContentFlag.project_id == scope.project_id, ContentFlag.id == flag_id))
    if flag is None:
        raise NotFoundInScope("flag")
    if flag.reviewed_at is not None:
        raise InvalidOperation("this flag was already reviewed")
    flag.reviewed_by, flag.reviewed_at = scope.actor.id, datetime.now(UTC)
    flag.review_note = (note or "").strip()[:2000] or None
    session.flush()
    return flag
