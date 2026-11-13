"""Tenancy and identity: users, tenants, projects, memberships.

Revision ID: 0001_tenancy
Revises:
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_tenancy"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("auth_issuer", sa.String(512), nullable=False),
        sa.Column("auth_subject", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255)),
        sa.Column("email", sa.String(320)),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("auth_issuer", "auth_subject", name="uq_users_auth_issuer"),
    )

    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_tenants_created_by_users"),
    )

    op.create_table(
        "tenant_memberships",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", name="pk_tenant_memberships"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_tenant_memberships_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_tenant_memberships_user_id_users"
        ),
        sa.CheckConstraint(
            "role IN ('admin', 'member')", name="ck_tenant_memberships_tenant_role"
        ),
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(2000)),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_projects_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_projects_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_projects_created_by_users"),
    )
    op.create_index("ix_projects_tenant_id", "projects", ["tenant_id"])

    op.create_table(
        "project_memberships",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("project_id", "user_id", name="pk_project_memberships"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
            name="fk_project_memberships_project_in_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.user_id"],
            ondelete="CASCADE",
            name="fk_project_memberships_user_in_tenant",
        ),
        sa.CheckConstraint(
            "role IN ('viewer', 'editor', 'approver', 'owner')",
            name="ck_project_memberships_project_role",
        ),
    )


def downgrade() -> None:
    op.drop_table("project_memberships")
    op.drop_index("ix_projects_tenant_id", table_name="projects")
    op.drop_table("projects")
    op.drop_table("tenant_memberships")
    op.drop_table("tenants")
    op.drop_table("users")
