import uuid

from fastapi import APIRouter, status

from app.api.deps import ActorDep, ProjectScopeDep, SessionDep, TenantScopeDep
from app.api.schemas import (
    MeOut,
    ProjectCreate,
    ProjectMemberSet,
    ProjectOut,
    ProjectUpdate,
    TenantCreate,
    TenantMemberSet,
    TenantOut,
)
from app.domain import tenancy
from app.domain.access import ProjectRole

router = APIRouter()


@router.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/me", tags=["identity"])
def me(actor: ActorDep) -> MeOut:
    return MeOut(user_id=actor.id)


@router.get("/tenants", tags=["tenants"])
def list_tenants(session: SessionDep, actor: ActorDep) -> list[TenantOut]:
    return [TenantOut(id=t.id, name=t.name, role=t.role) for t in tenancy.list_tenants(session, actor)]


@router.post("/tenants", status_code=status.HTTP_201_CREATED, tags=["tenants"])
def create_tenant(body: TenantCreate, session: SessionDep, actor: ActorDep) -> TenantOut:
    t = tenancy.create_tenant(session, actor, body.name)
    return TenantOut(id=t.id, name=t.name, role=t.role)


@router.put(
    "/tenants/{tenant_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["tenants"],
)
def set_tenant_member(
    user_id: uuid.UUID, body: TenantMemberSet, session: SessionDep, scope: TenantScopeDep
) -> None:
    tenancy.set_tenant_member(session, scope, user_id, body.role)


@router.get("/tenants/{tenant_id}/projects", tags=["projects"])
def list_projects(session: SessionDep, scope: TenantScopeDep) -> list[ProjectOut]:
    return [_project_out(p, role) for p, role in tenancy.list_projects(session, scope)]


@router.post(
    "/tenants/{tenant_id}/projects", status_code=status.HTTP_201_CREATED, tags=["projects"]
)
def create_project(body: ProjectCreate, session: SessionDep, scope: TenantScopeDep) -> ProjectOut:
    project = tenancy.create_project(session, scope, body.name, body.description)
    return _project_out(project, ProjectRole.OWNER)


@router.get("/tenants/{tenant_id}/projects/{project_id}", tags=["projects"])
def get_project(session: SessionDep, scope: ProjectScopeDep) -> ProjectOut:
    return _project_out(tenancy.get_project(session, scope), scope.role)


@router.patch("/tenants/{tenant_id}/projects/{project_id}", tags=["projects"])
def update_project(body: ProjectUpdate, session: SessionDep, scope: ProjectScopeDep) -> ProjectOut:
    project = tenancy.update_project(
        session, scope, name=body.name, description=body.description
    )
    return _project_out(project, scope.role)


@router.put(
    "/tenants/{tenant_id}/projects/{project_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["projects"],
)
def set_project_member(
    user_id: uuid.UUID, body: ProjectMemberSet, session: SessionDep, scope: ProjectScopeDep
) -> None:
    tenancy.set_project_member(session, scope, user_id, body.role)


def _project_out(project, role: ProjectRole) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        tenant_id=project.tenant_id,
        name=project.name,
        description=project.description,
        role=role,
    )
