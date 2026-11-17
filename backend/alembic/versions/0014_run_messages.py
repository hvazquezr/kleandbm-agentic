"""Agent run transcripts in one table

Revision ID: 0014_run_messages
Revises: 0013_accepted_exceptions
Create Date: 2026-11-16 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0014_run_messages'
down_revision: str | Sequence[str] | None = '0013_accepted_exceptions'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'run_messages',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('run_id', sa.Uuid(), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('segment', sa.String(length=200), nullable=True),
        sa.Column('content', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id', 'project_id', 'run_id'],
                                ['agent_runs.tenant_id', 'agent_runs.project_id', 'agent_runs.id'],
                                name='fk_run_messages_run', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_run_messages')),
        sa.UniqueConstraint('tenant_id', 'project_id', 'run_id', 'ordinal', name='uq_run_messages_ordinal'),
    )


def downgrade() -> None:
    op.drop_table('run_messages')
