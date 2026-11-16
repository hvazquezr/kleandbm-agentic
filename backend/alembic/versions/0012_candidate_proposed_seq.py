"""Design candidates pin the watermark they were proposed at

Revision ID: 0012_candidate_proposed_seq
Revises: 0011_decision_point_resolution
Create Date: 2026-11-16 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0012_candidate_proposed_seq'
down_revision: str | Sequence[str] | None = '0011_decision_point_resolution'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('design_candidates', sa.Column('proposed_seq', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('design_candidates', 'proposed_seq')
