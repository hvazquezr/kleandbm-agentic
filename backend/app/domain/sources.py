"""Source connections and investigation evidence.

- Connections are project-scoped. Creating or changing one needs PROJECT_MANAGE_SOURCES
  (owners), because they hold credentials. Running investigation tools needs PROJECT_EDIT,
  because every call writes evidence.
- Passwords are encrypted at rest and only decrypted to start a source session.
- Every tool call, including failures, is recorded as evidence with its arguments, result,
  schema fingerprint, and actor. Evidence is append-only.
- Hosts must be on the configured allow-list, so a connection can't make the backend probe
  arbitrary networks.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.sources import ALLOWED_TOOLS, ConnectionParams, SourceConnector, SourceSession, ToolResult
from app.domain.access import NotFoundInScope, Permission, ProjectScope
from app.domain.tenancy import InvalidOperation
from app.persistence.sources import SourceConnection, SourceEvidence, SourceKind


class SecretBox:
    """Encrypts secrets at rest with a Fernet key from settings."""

    def __init__(self, key: str) -> None:
        if not key:
            raise InvalidOperation("KLEANDBM_SECRETS_KEY is not configured; source credentials can't be stored")
        self._fernet = Fernet(key.encode())

    def seal(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def open(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise InvalidOperation("stored credentials can't be decrypted with the current key") from exc


def _actor_user(scope: ProjectScope) -> uuid.UUID:
    return scope.actor.on_behalf_of or scope.actor.id


# --- Connections ----------------------------------------------------------------------------


def create_connection(
    session: Session,
    scope: ProjectScope,
    box: SecretBox,
    allowed_hosts: list[str],
    *,
    name: str,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
) -> SourceConnection:
    scope.require(Permission.PROJECT_MANAGE_SOURCES)
    host = host.strip().lower()
    if host not in {h.lower() for h in allowed_hosts}:
        raise InvalidOperation(f"host {host!r} is not on the allowed source host list")
    if not (1 <= port <= 65535):
        raise InvalidOperation("port must be between 1 and 65535")
    existing = session.scalar(
        select(SourceConnection).where(SourceConnection.project_id == scope.project_id, SourceConnection.name == name)
    )
    if existing is not None:
        raise InvalidOperation(f"a source named {name!r} already exists in this project")
    connection = SourceConnection(
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        name=name.strip(),
        kind=SourceKind.MYSQL,
        host=host,
        port=port,
        database=database.strip(),
        username=username.strip(),
        password_encrypted=box.seal(password),
        created_by=_actor_user(scope),
    )
    session.add(connection)
    session.flush()
    return connection


def list_connections(session: Session, scope: ProjectScope) -> list[SourceConnection]:
    scope.require(Permission.PROJECT_READ)
    return list(
        session.scalars(
            select(SourceConnection)
            .where(SourceConnection.tenant_id == scope.tenant_id, SourceConnection.project_id == scope.project_id)
            .order_by(SourceConnection.name)
        )
    )


def get_connection(session: Session, scope: ProjectScope, connection_id: uuid.UUID) -> SourceConnection:
    scope.require(Permission.PROJECT_READ)
    connection = session.scalar(
        select(SourceConnection).where(
            SourceConnection.tenant_id == scope.tenant_id,
            SourceConnection.project_id == scope.project_id,
            SourceConnection.id == connection_id,
        )
    )
    if connection is None:
        raise NotFoundInScope("source connection")
    return connection


# --- Sessions and evidence ------------------------------------------------------------------


class InvestigationSession:
    """An open session against one source, recording every call as evidence."""

    def __init__(self, session: Session, scope: ProjectScope, connection: SourceConnection, source: SourceSession) -> None:
        self._db, self._scope, self._connection, self._source = session, scope, connection, source

    def call(self, tool: str, arguments: dict[str, Any], purpose: str | None = None) -> SourceEvidence:
        if tool not in ALLOWED_TOOLS:
            raise InvalidOperation(f"unknown tool {tool!r}")
        result: ToolResult = self._source.call(tool, arguments)
        actor = self._scope.actor
        evidence = SourceEvidence(
            tenant_id=self._scope.tenant_id,
            project_id=self._scope.project_id,
            connection_id=self._connection.id,
            tool=tool,
            arguments=arguments,
            result=result.data,
            is_error=result.is_error,
            error=result.error,
            truncated=result.truncated,
            schema_fingerprint=result.schema_fingerprint,
            purpose=(purpose or None) and purpose[:500],
            actor_kind=actor.kind,
            actor_id=actor.id,
            on_behalf_of=actor.on_behalf_of,
        )
        self._db.add(evidence)
        if result.schema_fingerprint:
            self._connection.last_schema_fingerprint = result.schema_fingerprint
            self._connection.last_verified_at = datetime.now(UTC)
        self._db.flush()
        return evidence


@contextmanager
def investigate(
    session: Session,
    scope: ProjectScope,
    connector: SourceConnector,
    box: SecretBox,
    connection_id: uuid.UUID,
) -> Iterator[InvestigationSession]:
    """Open one source session (one MCP server process) for a series of tool calls."""
    scope.require(Permission.PROJECT_EDIT)
    connection = get_connection(session, scope, connection_id)
    params = ConnectionParams(
        host=connection.host, port=connection.port, database=connection.database,
        username=connection.username, password=box.open(connection.password_encrypted),
    )
    with connector.open(params) as source:
        yield InvestigationSession(session, scope, connection, source)


def list_evidence(
    session: Session, scope: ProjectScope, connection_id: uuid.UUID | None = None, limit: int = 100
) -> list[SourceEvidence]:
    scope.require(Permission.PROJECT_READ)
    query = select(SourceEvidence).where(
        SourceEvidence.tenant_id == scope.tenant_id, SourceEvidence.project_id == scope.project_id
    )
    if connection_id is not None:
        query = query.where(SourceEvidence.connection_id == connection_id)
    return list(session.scalars(query.order_by(SourceEvidence.created_at.desc()).limit(max(1, min(limit, 500)))))


def get_evidence(session: Session, scope: ProjectScope, evidence_id: uuid.UUID) -> SourceEvidence:
    scope.require(Permission.PROJECT_READ)
    evidence = session.scalar(
        select(SourceEvidence).where(
            SourceEvidence.tenant_id == scope.tenant_id,
            SourceEvidence.project_id == scope.project_id,
            SourceEvidence.id == evidence_id,
        )
    )
    if evidence is None:
        raise NotFoundInScope("evidence")
    return evidence
