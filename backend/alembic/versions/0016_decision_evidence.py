"""Decisions record the evidence they rest on

Revision ID: 0016_decision_evidence
Revises: 0015_run_assessments
Create Date: 2026-11-17 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0016_decision_evidence'
down_revision: str | Sequence[str] | None = '0015_run_assessments'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing revisions recorded no evidence; the default only fills them in.
    op.add_column('decision_revisions', sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()),
                                                  nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.alter_column('decision_revisions', 'evidence', server_default=None)


def downgrade() -> None:
    op.drop_column('decision_revisions', 'evidence')
