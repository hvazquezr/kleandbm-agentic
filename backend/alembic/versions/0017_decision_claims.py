"""Decisions record typed claims about the source, each with its evidence

Revision ID: 0017_decision_claims
Revises: 0016_decision_evidence
Create Date: 2026-11-17 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0017_decision_claims'
down_revision: str | Sequence[str] | None = '0016_decision_evidence'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing revisions recorded no claims; the default only fills them in.
    op.add_column('decision_revisions', sa.Column('claims', postgresql.JSONB(astext_type=sa.Text()),
                                                  nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.alter_column('decision_revisions', 'claims', server_default=None)


def downgrade() -> None:
    op.drop_column('decision_revisions', 'claims')
