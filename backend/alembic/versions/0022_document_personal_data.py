"""Documents: how many personal-data values were masked in a version's passages

Revision ID: 0022_document_personal_data
Revises: 0021_one_conversation_per_model
Create Date: 2026-11-19 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0022_document_personal_data'
down_revision: str | Sequence[str] | None = '0021_one_conversation_per_model'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL for versions processed before masking existed: scripts/reprocess_documents.py finds them.
    op.add_column('document_versions', sa.Column('personal_data_masked', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('document_versions', 'personal_data_masked')
