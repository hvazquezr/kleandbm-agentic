"""Actors, roles, permissions, and the scope objects every domain operation receives.

Role names and their permission sets are provisional (spec §8 leaves exact roles open).
Code should check permissions, never role names, so the mapping can change in one place.
"""

import enum
import uuid
from dataclasses import dataclass


class ActorKind(enum.StrEnum):
    USER = "user"
    AGENT = "agent"


@dataclass(frozen=True)
class Actor:
    """Who is acting. Agent actors always act on behalf of an initiating user."""

    kind: ActorKind
    id: uuid.UUID
    on_behalf_of: uuid.UUID | None = None


class TenantRole(enum.StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class ProjectRole(enum.StrEnum):
    VIEWER = "viewer"
    EDITOR = "editor"
    APPROVER = "approver"
    OWNER = "owner"


class Permission(enum.StrEnum):
    PROJECT_READ = "project:read"
    PROJECT_EDIT = "project:edit"
    # Authority to confirm business meanings and accept a model (spec §7). Never granted to agents.
    PROJECT_APPROVE = "project:approve"
    PROJECT_MANAGE_MEMBERS = "project:manage_members"
    # Add or change source connections, which hold credentials. Owner only.
    PROJECT_MANAGE_SOURCES = "project:manage_sources"
    TENANT_CREATE_PROJECT = "tenant:create_project"
    TENANT_MANAGE_MEMBERS = "tenant:manage_members"


PROJECT_ROLE_PERMISSIONS: dict[ProjectRole, frozenset[Permission]] = {
    ProjectRole.VIEWER: frozenset({Permission.PROJECT_READ}),
    ProjectRole.EDITOR: frozenset({Permission.PROJECT_READ, Permission.PROJECT_EDIT}),
    ProjectRole.APPROVER: frozenset(
        {Permission.PROJECT_READ, Permission.PROJECT_EDIT, Permission.PROJECT_APPROVE}
    ),
    ProjectRole.OWNER: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.PROJECT_EDIT,
            Permission.PROJECT_APPROVE,
            Permission.PROJECT_MANAGE_MEMBERS,
            Permission.PROJECT_MANAGE_SOURCES,
        }
    ),
}

TENANT_ROLE_PERMISSIONS: dict[TenantRole, frozenset[Permission]] = {
    TenantRole.MEMBER: frozenset({Permission.TENANT_CREATE_PROJECT}),
    TenantRole.ADMIN: frozenset(
        {Permission.TENANT_CREATE_PROJECT, Permission.TENANT_MANAGE_MEMBERS}
    ),
}

AGENT_FORBIDDEN_PERMISSIONS = frozenset({Permission.PROJECT_APPROVE})


class AccessDenied(Exception):
    """The actor is authenticated and in scope but lacks the required permission."""


class NotFoundInScope(Exception):
    """The resource does not exist or is outside the actor's scope.

    Both cases raise this, so callers can't probe for resources in other tenants or projects.
    """


@dataclass(frozen=True)
class TenantScope:
    actor: Actor
    tenant_id: uuid.UUID
    role: TenantRole

    @property
    def permissions(self) -> frozenset[Permission]:
        return TENANT_ROLE_PERMISSIONS[self.role]

    def require(self, permission: Permission) -> None:
        if permission not in self.permissions:
            raise AccessDenied(permission)


@dataclass(frozen=True)
class ProjectScope:
    actor: Actor
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    role: ProjectRole

    @property
    def permissions(self) -> frozenset[Permission]:
        granted = PROJECT_ROLE_PERMISSIONS[self.role]
        if self.actor.kind is ActorKind.AGENT:
            granted = granted - AGENT_FORBIDDEN_PERMISSIONS
        return granted

    def require(self, permission: Permission) -> None:
        if permission not in self.permissions:
            raise AccessDenied(permission)
