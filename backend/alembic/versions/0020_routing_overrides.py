"""Routing overrides: the earlier turn a user's "send to the other agent" overrode

Revision ID: 0020_routing_overrides
Revises: 0019_routing_decisions
Create Date: 2026-11-18 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0020_routing_overrides'
down_revision: str | Sequence[str] | None = '0019_routing_decisions'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('routing_decisions', sa.Column('overrides_run_id', sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column('routing_decisions', 'overrides_run_id')
