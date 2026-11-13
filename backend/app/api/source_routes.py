"""Source connections, investigation tool calls, and evidence."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from app.adapters.sources import ALLOWED_TOOLS, SourceConnector
from app.api.deps import ProjectScopeDep, SessionDep
from app.config import get_settings
from app.domain import sources
from app.domain.tenancy import InvalidOperation
from app.persistence.sources import SourceConnection, SourceEvidence

router = APIRouter(prefix="/tenants/{tenant_id}/projects/{project_id}", tags=["sources"])


def get_connector(request: Request) -> SourceConnector:
    return request.app.state.source_connector


def get_box() -> sources.SecretBox:
    return sources.SecretBox(get_settings().secrets_key)


ConnectorDep = Annotated[SourceConnector, Depends(get_connector)]
BoxDep = Annotated[sources.SecretBox, Depends(get_box)]


class ConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=255)
    port: int = 3306
    database: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(max_length=512)


class ConnectionOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    host: str
    port: int
    database: str
    username: str
    last_schema_fingerprint: str | None
    last_verified_at: datetime | None
    created_at: datetime


class ToolCallIn(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    purpose: str | None = Field(default=None, max_length=500)


class EvidenceOut(BaseModel):
    id: uuid.UUID
    connection_id: uuid.UUID
    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    is_error: bool
    error: str | None
    truncated: bool
    schema_fingerprint: str | None
    purpose: str | None
    actor_kind: str
    actor_id: uuid.UUID
    on_behalf_of: uuid.UUID | None
    created_at: datetime


def _conn_out(c: SourceConnection) -> ConnectionOut:
    return ConnectionOut(
        id=c.id, name=c.name, kind=str(c.kind), host=c.host, port=c.port, database=c.database,
        username=c.username, last_schema_fingerprint=c.last_schema_fingerprint,
        last_verified_at=c.last_verified_at, created_at=c.created_at,
    )


def _evidence_out(e: SourceEvidence) -> EvidenceOut:
    return EvidenceOut(
        id=e.id, connection_id=e.connection_id, tool=e.tool, arguments=e.arguments, result=e.result,
        is_error=e.is_error, error=e.error, truncated=e.truncated, schema_fingerprint=e.schema_fingerprint,
        purpose=e.purpose, actor_kind=str(e.actor_kind), actor_id=e.actor_id, on_behalf_of=e.on_behalf_of,
        created_at=e.created_at,
    )


@router.get("/sources")
def list_sources(session: SessionDep, scope: ProjectScopeDep) -> list[ConnectionOut]:
    return [_conn_out(c) for c in sources.list_connections(session, scope)]


@router.post("/sources", status_code=status.HTTP_201_CREATED)
def create_source(body: ConnectionIn, session: SessionDep, scope: ProjectScopeDep, box: BoxDep) -> ConnectionOut:
    connection = sources.create_connection(
        session, scope, box, get_settings().source_allowed_hosts,
        name=body.name, host=body.host, port=body.port, database=body.database,
        username=body.username, password=body.password,
    )
    return _conn_out(connection)


@router.post("/sources/{connection_id}/tools/{tool}")
def call_tool(
    connection_id: uuid.UUID,
    tool: str,
    body: ToolCallIn,
    session: SessionDep,
    scope: ProjectScopeDep,
    connector: ConnectorDep,
    box: BoxDep,
) -> EvidenceOut:
    """Run one curated tool. The result, success or error, is recorded as evidence and returned."""
    if tool not in ALLOWED_TOOLS:
        raise InvalidOperation(f"unknown tool {tool!r}; available: {sorted(ALLOWED_TOOLS)}")
    with sources.investigate(session, scope, connector, box, connection_id) as inv:
        return _evidence_out(inv.call(tool, body.arguments, body.purpose))


@router.get("/sources/{connection_id}/evidence")
def list_source_evidence(
    connection_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep, limit: int = 100
) -> list[EvidenceOut]:
    sources.get_connection(session, scope, connection_id)
    return [_evidence_out(e) for e in sources.list_evidence(session, scope, connection_id, limit)]


@router.get("/evidence/{evidence_id}")
def get_evidence(evidence_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> EvidenceOut:
    return _evidence_out(sources.get_evidence(session, scope, evidence_id))
