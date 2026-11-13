import uuid

import pytest

from app.domain.access import (
    AccessDenied,
    Actor,
    ActorKind,
    Permission,
    ProjectRole,
    ProjectScope,
)


def _scope(role: ProjectRole, kind: ActorKind = ActorKind.USER) -> ProjectScope:
    user = uuid.uuid4()
    actor = Actor(kind=kind, id=uuid.uuid4() if kind is ActorKind.AGENT else user,
                  on_behalf_of=user if kind is ActorKind.AGENT else None)
    return ProjectScope(actor=actor, tenant_id=uuid.uuid4(), project_id=uuid.uuid4(), role=role)


def test_viewer_is_read_only():
    scope = _scope(ProjectRole.VIEWER)
    scope.require(Permission.PROJECT_READ)
    with pytest.raises(AccessDenied):
        scope.require(Permission.PROJECT_EDIT)


def test_editor_cannot_approve():
    with pytest.raises(AccessDenied):
        _scope(ProjectRole.EDITOR).require(Permission.PROJECT_APPROVE)


def test_approver_can_approve():
    _scope(ProjectRole.APPROVER).require(Permission.PROJECT_APPROVE)


@pytest.mark.parametrize("role", [ProjectRole.APPROVER, ProjectRole.OWNER])
def test_agent_never_gets_approval_authority(role):
    # Spec §7: no agent may confirm business meanings or accept a model for the user.
    scope = _scope(role, ActorKind.AGENT)
    scope.require(Permission.PROJECT_EDIT)
    with pytest.raises(AccessDenied):
        scope.require(Permission.PROJECT_APPROVE)
