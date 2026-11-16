"""Accepted events record the unconfirmed definitions accepted as exceptions

Revision ID: 0013_accepted_exceptions
Revises: 0012_candidate_proposed_seq
Create Date: 2026-11-16 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0013_accepted_exceptions'
down_revision: str | Sequence[str] | None = '0012_candidate_proposed_seq'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('model_state_events', sa.Column(
        'accepted_exceptions', postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"), nullable=False))


def downgrade() -> None:
    op.drop_column('model_state_events', 'accepted_exceptions')
