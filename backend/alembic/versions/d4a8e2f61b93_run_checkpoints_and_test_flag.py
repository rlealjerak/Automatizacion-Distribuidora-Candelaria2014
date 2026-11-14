"""run checkpoints (raw_line_items.processed_at, list_runs.last_progress_at) and suppliers.is_test

Revision ID: d4a8e2f61b93
Revises: c3f7a9d1e52b
Create Date: 2026-11-14 00:00:00.000000

Additive only - three nullable/defaulted columns, no ALTER TYPE, no
renames, no data rewrites. Old code simply never reads them.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a8e2f61b93'
down_revision: Union[str, None] = 'c3f7a9d1e52b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('raw_line_items', sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('list_runs', sa.Column('last_progress_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('suppliers', sa.Column('is_test', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('suppliers', 'is_test')
    op.drop_column('list_runs', 'last_progress_at')
    op.drop_column('raw_line_items', 'processed_at')
