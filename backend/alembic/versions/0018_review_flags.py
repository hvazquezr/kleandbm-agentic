"""Reviewer verdicts on monitor assessments, and flags on untrusted content

Revision ID: 0018_review_flags
Revises: 0017_decision_claims
Create Date: 2026-11-17 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0018_review_flags'
down_revision: str | Sequence[str] | None = '0017_decision_claims'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint('uq_run_assessments_scope', 'run_assessments', ['tenant_id', 'project_id', 'id'])
    op.create_table(
        'assessment_reviews',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('assessment_id', sa.Uuid(), nullable=False),
        sa.Column('verdict', sa.String(length=24), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('reviewed_by', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id', 'project_id', 'assessment_id'],
                                ['run_assessments.tenant_id', 'run_assessments.project_id', 'run_assessments.id'],
                                name='fk_assessment_reviews_assessment', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_assessment_reviews')),
    )
    op.create_index('ix_assessment_reviews_assessment', 'assessment_reviews', ['tenant_id', 'project_id', 'assessment_id'])
    op.create_table(
        'content_flags',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('run_id', sa.Uuid(), nullable=False),
        sa.Column('agent', sa.String(length=40), nullable=False),
        sa.Column('target_kind', sa.String(length=16), nullable=False),
        sa.Column('document_version_id', sa.Uuid(), nullable=True),
        sa.Column('location', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('source_id', sa.Uuid(), nullable=True),
        sa.Column('source_table', sa.String(length=200), nullable=True),
        sa.Column('source_column', sa.String(length=200), nullable=True),
        sa.Column('quote', sa.Text(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('reviewed_by', sa.Uuid(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('review_note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id', 'project_id', 'run_id'],
                                ['agent_runs.tenant_id', 'agent_runs.project_id', 'agent_runs.id'],
                                name='fk_content_flags_run', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id', 'project_id', 'document_version_id'],
                                ['document_versions.tenant_id', 'document_versions.project_id', 'document_versions.id'],
                                name='fk_content_flags_document_version'),
        sa.ForeignKeyConstraint(['tenant_id', 'project_id', 'source_id'],
                                ['source_connections.tenant_id', 'source_connections.project_id', 'source_connections.id'],
                                name='fk_content_flags_source'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_content_flags')),
    )
    op.create_index('ix_content_flags_project', 'content_flags', ['tenant_id', 'project_id'])


def downgrade() -> None:
    op.drop_index('ix_content_flags_project', table_name='content_flags')
    op.drop_table('content_flags')
    op.drop_index('ix_assessment_reviews_assessment', table_name='assessment_reviews')
    op.drop_table('assessment_reviews')
    op.drop_constraint('uq_run_assessments_scope', 'run_assessments', type_='unique')
