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
from app.persistence.sources import ModelSource, SourceConnection, SourceEvidence, SourceKind


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


class ConnectionRejected(InvalidOperation):
    """The connection works but can't be used safely (e.g. the account can write)."""


def _first_leaf(exc: BaseException) -> BaseException:
    """anyio wraps task failures in exception groups; report the underlying error instead."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def verify_params(connector: SourceConnector, params: ConnectionParams) -> dict[str, Any]:
    """Connect and confirm the account is strictly read-only. Raises InvalidOperation otherwise.

    Runs before credentials are stored and before every investigation session, because
    grants can change after a connection is saved.
    """
    try:
        with connector.open(params) as source:
            privileges = source.call("account_privileges", {})
            if privileges.is_error:
                error = privileges.error or ""
                raise InvalidOperation(error if error.startswith("could not connect") else
                                       f"could not check the account's privileges: {error}")
            check = privileges.data or {}
            if not check.get("read_only"):
                problems = "; ".join(check.get("problems") or ["unknown privileges"])
                raise ConnectionRejected(
                    f"this account isn't read-only ({problems}). Use an account with only SELECT "
                    f"(and SHOW VIEW) on {params.database}."
                )
            tables = source.call("list_tables", {})
            if tables.is_error:
                raise InvalidOperation(f"connected, but couldn't list tables: {tables.error}")
    except Exception as exc:  # also the server process failing to start, transport errors
        root = _first_leaf(exc)
        if isinstance(root, InvalidOperation):  # ours, possibly wrapped by the session's task group
            raise root from None
        raise InvalidOperation(f"could not connect: {type(root).__name__}: {root}"[:500]) from exc
    return {
        "read_only": True,
        "warnings": check.get("warnings") or [],
        "grants": check.get("grants") or [],
        "tables": len((tables.data or {}).get("tables", [])),
        "schema_fingerprint": tables.schema_fingerprint,
    }


def _validate_target(allowed_hosts: list[str], host: str, port: int) -> str:
    host = host.strip().lower()
    if host not in {h.lower() for h in allowed_hosts}:
        raise InvalidOperation(f"host {host!r} is not on the allowed source host list")
    if not (1 <= port <= 65535):
        raise InvalidOperation("port must be between 1 and 65535")
    return host


def _record_check(connection: SourceConnection, check: dict[str, Any]) -> None:
    connection.privilege_check = {k: check[k] for k in ("read_only", "warnings", "grants", "tables")}
    connection.last_schema_fingerprint = check.get("schema_fingerprint") or connection.last_schema_fingerprint
    connection.last_verified_at = datetime.now(UTC)


def create_connection(
    session: Session,
    scope: ProjectScope,
    box: SecretBox,
    connector: SourceConnector,
    allowed_hosts: list[str],
    *,
    name: str,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
) -> SourceConnection:
    """Verify, then store. Credentials that can't connect, or that can write, are never stored."""
    scope.require(Permission.PROJECT_MANAGE_SOURCES)
    host = _validate_target(allowed_hosts, host, port)
    name = name.strip()
    existing = session.scalar(
        select(SourceConnection).where(SourceConnection.project_id == scope.project_id, SourceConnection.name == name)
    )
    if existing is not None:
        raise InvalidOperation(f"a source named {name!r} already exists in this project")
    params = ConnectionParams(host=host, port=port, database=database.strip(), username=username.strip(), password=password)
    check = verify_params(connector, params)
    connection = SourceConnection(
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        name=name,
        kind=SourceKind.MYSQL,
        host=host,
        port=port,
        database=params.database,
        username=params.username,
        password_encrypted=box.seal(password),
        created_by=_actor_user(scope),
    )
    _record_check(connection, check)
    session.add(connection)
    session.flush()
    return connection


def update_connection(
    session: Session,
    scope: ProjectScope,
    box: SecretBox,
    connector: SourceConnector,
    allowed_hosts: list[str],
    connection_id: uuid.UUID,
    *,
    host: str | None = None,
    port: int | None = None,
    database: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> SourceConnection:
    """Change or rotate credentials. The new settings are verified before they replace the old."""
    scope.require(Permission.PROJECT_MANAGE_SOURCES)
    connection = get_connection(session, scope, connection_id)
    new_host = _validate_target(allowed_hosts, host or connection.host, port or connection.port)
    params = ConnectionParams(
        host=new_host,
        port=port or connection.port,
        database=(database or connection.database).strip(),
        username=(username or connection.username).strip(),
        password=password if password is not None else box.open(connection.password_encrypted),
    )
    check = verify_params(connector, params)
    connection.host, connection.port = params.host, params.port
    connection.database, connection.username = params.database, params.username
    if password is not None:
        connection.password_encrypted = box.seal(password)
    connection.disabled_at = None
    _record_check(connection, check)
    session.flush()
    return connection


def test_connection(
    session: Session, scope: ProjectScope, box: SecretBox, connector: SourceConnector, connection_id: uuid.UUID
) -> SourceConnection:
    scope.require(Permission.PROJECT_EDIT)
    connection = get_connection(session, scope, connection_id)
    _record_check(connection, verify_params(connector, _params(connection, box)))
    session.flush()
    return connection


def disable_connection(session: Session, scope: ProjectScope, connection_id: uuid.UUID) -> SourceConnection:
    """Stop using a connection. It's kept, with its evidence, for traceability."""
    scope.require(Permission.PROJECT_MANAGE_SOURCES)
    connection = get_connection(session, scope, connection_id)
    connection.disabled_at = connection.disabled_at or datetime.now(UTC)
    session.flush()
    return connection


def _params(connection: SourceConnection, box: SecretBox) -> ConnectionParams:
    return ConnectionParams(
        host=connection.host, port=connection.port, database=connection.database,
        username=connection.username, password=box.open(connection.password_encrypted),
    )


# --- Model links ----------------------------------------------------------------------------


def link_model_source(session: Session, scope: ProjectScope, model_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    from app.domain import modeling

    scope.require(Permission.PROJECT_EDIT)
    modeling.get_model(session, scope, model_id)
    connection = get_connection(session, scope, connection_id)
    if connection.disabled_at:
        raise InvalidOperation("that source is disabled")
    if session.get(ModelSource, (model_id, connection_id)) is None:
        session.add(ModelSource(tenant_id=scope.tenant_id, project_id=scope.project_id, model_id=model_id,
                                connection_id=connection_id, linked_by=_actor_user(scope)))
        session.flush()


def unlink_model_source(session: Session, scope: ProjectScope, model_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    scope.require(Permission.PROJECT_EDIT)
    link = session.scalar(select(ModelSource).where(
        ModelSource.project_id == scope.project_id, ModelSource.model_id == model_id,
        ModelSource.connection_id == connection_id))
    if link is None:
        raise NotFoundInScope("model source link")
    session.delete(link)
    session.flush()


def model_sources(session: Session, scope: ProjectScope, model_id: uuid.UUID) -> list[SourceConnection]:
    from app.domain import modeling

    modeling.get_model(session, scope, model_id)
    return list(session.scalars(
        select(SourceConnection)
        .join(ModelSource, (ModelSource.connection_id == SourceConnection.id)
              & (ModelSource.project_id == SourceConnection.project_id))
        .where(ModelSource.tenant_id == scope.tenant_id, ModelSource.project_id == scope.project_id,
               ModelSource.model_id == model_id)
        .order_by(SourceConnection.name)
    ))


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
    if connection.disabled_at:
        raise InvalidOperation(f"source {connection.name!r} is disabled")
    params = _params(connection, box)
    with connector.open(params) as source:
        # Grants can change after a connection is saved; re-check on every session.
        privileges = source.call("account_privileges", {})
        if privileges.is_error or not (privileges.data or {}).get("read_only"):
            problems = "; ".join((privileges.data or {}).get("problems") or [privileges.error or "unknown"])
            connection.privilege_check = {**(connection.privilege_check or {}), "read_only": False, "problems": [problems]}
            session.flush()
            raise ConnectionRejected(f"source {connection.name!r} is no longer read-only ({problems}); fix its grants and test it again")
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
