"""Decision point resolution: a person decides a search that ended without a selection

Revision ID: 0011_decision_point_resolution
Revises: 0010_search
Create Date: 2026-11-13 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0011_decision_point_resolution'
down_revision: str | Sequence[str] | None = '0010_search'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = ("clarification", "contradiction", "missing_information", "validation_finding")


def _kind_check(kinds: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_question_revisions_question_kind"), "question_revisions", type_="check")
    values = ", ".join(f"'{k}'" for k in kinds)
    op.create_check_constraint(op.f("ck_question_revisions_question_kind"), "question_revisions", f"kind IN ({values})")


def upgrade() -> None:
    op.add_column('decision_points', sa.Column('question_id', sa.Uuid(), nullable=True))
    op.add_column('decision_points', sa.Column('resolved_by', sa.Uuid(), nullable=True))
    op.add_column('decision_points', sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('decision_points', sa.Column('resolution', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_foreign_key('fk_decision_points_resolved_by', 'decision_points', 'users', ['resolved_by'], ['id'])
    _kind_check((*_KINDS, "decision_point"))


def downgrade() -> None:
    op.execute("UPDATE question_revisions SET kind = 'clarification' WHERE kind = 'decision_point'")
    _kind_check(_KINDS)
    op.drop_constraint('fk_decision_points_resolved_by', 'decision_points', type_='foreignkey')
    op.drop_column('decision_points', 'resolution')
    op.drop_column('decision_points', 'resolved_at')
    op.drop_column('decision_points', 'resolved_by')
    op.drop_column('decision_points', 'question_id')
