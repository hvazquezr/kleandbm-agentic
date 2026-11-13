"""Tenant, project, and membership operations.

Every function either resolves a scope from the actor's current memberships or takes a
scope that was resolved that way. Nothing here accepts a bare tenant or project id from a
caller and trusts it.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.access import (
    Actor,
    ActorKind,
    NotFoundInScope,
    Permission,
    ProjectRole,
    ProjectScope,
    TenantRole,
    TenantScope,
)
from app.persistence.models import (
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
    User,
)


class InvalidOperation(Exception):
    """The request is in scope and permitted but would leave data in an invalid state."""


@dataclass(frozen=True)
class TenantSummary:
    id: uuid.UUID
    name: str
    role: TenantRole


# --- Identity -------------------------------------------------------------------------------


def get_or_create_user(
    session: Session,
    *,
    issuer: str,
    subject: str,
    display_name: str | None = None,
    email: str | None = None,
) -> User:
    user = session.scalar(
        select(User).where(User.auth_issuer == issuer, User.auth_subject == subject)
    )
    if user is None:
        user = User(
            auth_issuer=issuer, auth_subject=subject, display_name=display_name, email=email
        )
        session.add(user)
        session.flush()
    return user


def _user_id(actor: Actor) -> uuid.UUID:
    # Agent actors are resolved against the memberships of the user they act for, and so
    # can never reach more than that user can.
    if actor.kind is ActorKind.AGENT:
        if actor.on_behalf_of is None:
            raise NotFoundInScope("agent actor has no initiating user")
        return actor.on_behalf_of
    return actor.id


# --- Scope resolution -----------------------------------------------------------------------


def resolve_tenant_scope(session: Session, actor: Actor, tenant_id: uuid.UUID) -> TenantScope:
    membership = session.get(TenantMembership, (tenant_id, _user_id(actor)))
    if membership is None:
        raise NotFoundInScope("tenant")
    return TenantScope(actor=actor, tenant_id=tenant_id, role=membership.role)


def resolve_project_scope(
    session: Session, actor: Actor, tenant_id: uuid.UUID, project_id: uuid.UUID
) -> ProjectScope:
    # Tenant membership alone never grants project access (spec §7), not even to tenant admins.
    membership = session.scalar(
        select(ProjectMembership).where(
            ProjectMembership.tenant_id == tenant_id,
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == _user_id(actor),
        )
    )
    if membership is None:
        raise NotFoundInScope("project")
    return ProjectScope(
        actor=actor, tenant_id=tenant_id, project_id=project_id, role=membership.role
    )


# --- Tenants --------------------------------------------------------------------------------


def create_tenant(session: Session, actor: Actor, name: str) -> TenantSummary:
    if actor.kind is not ActorKind.USER:
        raise InvalidOperation("only users can create tenants")
    tenant = Tenant(name=name, created_by=actor.id)
    session.add(tenant)
    session.flush()
    session.add(TenantMembership(tenant_id=tenant.id, user_id=actor.id, role=TenantRole.ADMIN))
    session.flush()
    return TenantSummary(id=tenant.id, name=tenant.name, role=TenantRole.ADMIN)


def list_tenants(session: Session, actor: Actor) -> list[TenantSummary]:
    rows = session.execute(
        select(Tenant.id, Tenant.name, TenantMembership.role)
        .join(TenantMembership, TenantMembership.tenant_id == Tenant.id)
        .where(TenantMembership.user_id == _user_id(actor))
        .order_by(Tenant.name)
    )
    return [TenantSummary(id=r.id, name=r.name, role=r.role) for r in rows]


def set_tenant_member(
    session: Session, scope: TenantScope, user_id: uuid.UUID, role: TenantRole
) -> None:
    scope.require(Permission.TENANT_MANAGE_MEMBERS)
    if session.get(User, user_id) is None:
        raise InvalidOperation("unknown user")
    membership = session.get(TenantMembership, (scope.tenant_id, user_id))
    if membership is None:
        session.add(TenantMembership(tenant_id=scope.tenant_id, user_id=user_id, role=role))
    else:
        if membership.role is TenantRole.ADMIN and role is not TenantRole.ADMIN:
            _ensure_other_tenant_admin(session, scope.tenant_id, user_id)
        membership.role = role
    session.flush()


def _ensure_other_tenant_admin(session: Session, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    others = session.scalar(
        select(func.count())
        .select_from(TenantMembership)
        .where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.role == TenantRole.ADMIN,
            TenantMembership.user_id != user_id,
        )
    )
    if not others:
        raise InvalidOperation("a tenant must keep at least one admin")


# --- Projects -------------------------------------------------------------------------------


def create_project(
    session: Session, scope: TenantScope, name: str, description: str | None = None
) -> Project:
    scope.require(Permission.TENANT_CREATE_PROJECT)
    creator = _user_id(scope.actor)
    project = Project(
        tenant_id=scope.tenant_id, name=name, description=description, created_by=creator
    )
    session.add(project)
    session.flush()
    session.add(
        ProjectMembership(
            tenant_id=scope.tenant_id,
            project_id=project.id,
            user_id=creator,
            role=ProjectRole.OWNER,
        )
    )
    session.flush()
    return project


def list_projects(session: Session, scope: TenantScope) -> list[tuple[Project, ProjectRole]]:
    """Projects in the tenant that the actor is a member of."""
    rows = session.execute(
        select(Project, ProjectMembership.role)
        .join(
            ProjectMembership,
            (ProjectMembership.project_id == Project.id)
            & (ProjectMembership.tenant_id == Project.tenant_id),
        )
        .where(
            Project.tenant_id == scope.tenant_id,
            ProjectMembership.user_id == _user_id(scope.actor),
        )
        .order_by(Project.name)
    )
    return [(r[0], r[1]) for r in rows]


def get_project(session: Session, scope: ProjectScope) -> Project:
    scope.require(Permission.PROJECT_READ)
    project = session.scalar(
        select(Project).where(
            Project.tenant_id == scope.tenant_id, Project.id == scope.project_id
        )
    )
    if project is None:
        raise NotFoundInScope("project")
    return project


def update_project(
    session: Session,
    scope: ProjectScope,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Project:
    scope.require(Permission.PROJECT_EDIT)
    project = get_project(session, scope)
    if name is not None:
        project.name = name
    if description is not None:
        project.description = description
    session.flush()
    return project


def set_project_member(
    session: Session, scope: ProjectScope, user_id: uuid.UUID, role: ProjectRole
) -> None:
    scope.require(Permission.PROJECT_MANAGE_MEMBERS)
    if session.get(TenantMembership, (scope.tenant_id, user_id)) is None:
        raise InvalidOperation("user must be a member of the project's tenant")
    membership = session.get(ProjectMembership, (scope.project_id, user_id))
    if membership is None:
        session.add(
            ProjectMembership(
                tenant_id=scope.tenant_id, project_id=scope.project_id, user_id=user_id, role=role
            )
        )
    else:
        if membership.role is ProjectRole.OWNER and role is not ProjectRole.OWNER:
            _ensure_other_project_owner(session, scope.project_id, user_id)
        membership.role = role
    session.flush()


def _ensure_other_project_owner(
    session: Session, project_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    others = session.scalar(
        select(func.count())
        .select_from(ProjectMembership)
        .where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.role == ProjectRole.OWNER,
            ProjectMembership.user_id != user_id,
        )
    )
    if not others:
        raise InvalidOperation("a project must keep at least one owner")
