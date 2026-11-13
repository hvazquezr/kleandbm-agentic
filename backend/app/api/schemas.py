import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.domain.access import ProjectRole, TenantRole


class MeOut(BaseModel):
    user_id: uuid.UUID


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    role: TenantRole


class TenantMemberSet(BaseModel):
    role: TenantRole


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    role: ProjectRole


class ProjectMemberSet(BaseModel):
    role: ProjectRole
