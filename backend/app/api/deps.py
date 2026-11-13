import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.api.auth import InvalidToken, TokenVerifier
from app.domain import tenancy
from app.domain.access import Actor, ActorKind, ProjectScope, TenantScope
from app.persistence.db import get_session

_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[Session, Depends(get_session)]


def get_token_verifier(request: Request) -> TokenVerifier:
    verifier: TokenVerifier | None = getattr(request.app.state, "token_verifier", None)
    if verifier is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication not configured")
    return verifier


def get_current_actor(
    session: SessionDep,
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Actor:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = verifier.verify(credentials.credentials)
    except InvalidToken:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from None
    user = tenancy.get_or_create_user(
        session,
        issuer=claims.issuer,
        subject=claims.subject,
        display_name=claims.name,
        email=claims.email,
    )
    return Actor(kind=ActorKind.USER, id=user.id)


ActorDep = Annotated[Actor, Depends(get_current_actor)]


def get_tenant_scope(tenant_id: uuid.UUID, session: SessionDep, actor: ActorDep) -> TenantScope:
    return tenancy.resolve_tenant_scope(session, actor, tenant_id)


def get_project_scope(
    tenant_id: uuid.UUID, project_id: uuid.UUID, session: SessionDep, actor: ActorDep
) -> ProjectScope:
    return tenancy.resolve_project_scope(session, actor, tenant_id, project_id)


TenantScopeDep = Annotated[TenantScope, Depends(get_tenant_scope)]
ProjectScopeDep = Annotated[ProjectScope, Depends(get_project_scope)]
