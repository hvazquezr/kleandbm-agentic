"""Request router decisions

Revision ID: 0019_routing_decisions
Revises: 0018_review_flags
Create Date: 2026-11-18 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0019_routing_decisions'
down_revision: str | Sequence[str] | None = '0018_review_flags'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'routing_decisions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('run_id', sa.Uuid(), nullable=False),
        sa.Column('route', sa.String(length=40), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('router_model', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id', 'project_id', 'run_id'],
                                ['agent_runs.tenant_id', 'agent_runs.project_id', 'agent_runs.id'],
                                name='fk_routing_decisions_run', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_routing_decisions')),
        sa.UniqueConstraint('tenant_id', 'project_id', 'run_id', name='uq_routing_decisions_run'),
    )


def downgrade() -> None:
    op.drop_table('routing_decisions')
