"""Transcript monitor assessments of agent runs

Revision ID: 0015_run_assessments
Revises: 0014_run_messages
Create Date: 2026-11-17 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0015_run_assessments'
down_revision: str | Sequence[str] | None = '0014_run_messages'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'run_assessments',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('run_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('monitor_model', sa.String(length=100), nullable=True),
        sa.Column('score', sa.Integer(), nullable=True),
        sa.Column('flagged', sa.Boolean(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('concerns', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('untrusted_instructions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('samples', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=False),
        sa.Column('output_tokens', sa.Integer(), nullable=False),
        sa.Column('truncated', sa.Boolean(), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id', 'project_id', 'run_id'],
                                ['agent_runs.tenant_id', 'agent_runs.project_id', 'agent_runs.id'],
                                name='fk_run_assessments_run', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_run_assessments')),
    )
    op.create_index('ix_run_assessments_run', 'run_assessments', ['tenant_id', 'project_id', 'run_id'])


def downgrade() -> None:
    op.drop_index('ix_run_assessments_run', table_name='run_assessments')
    op.drop_table('run_assessments')
