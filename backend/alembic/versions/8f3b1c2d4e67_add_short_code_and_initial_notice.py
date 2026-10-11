"""add short_code and initial_notice_sent_at to approval_queue

Revision ID: 8f3b1c2d4e67
Revises: 0b21449f1923
Create Date: 2026-10-11 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8f3b1c2d4e67'
down_revision: Union[str, None] = '0b21449f1923'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE approval_queue_short_code_seq")

    # Nullable first so existing rows (if any) can be backfilled below,
    # then tightened to NOT NULL + UNIQUE + a server-side default for
    # every future insert - never reused since it's a real sequence, not
    # a max()+1 computed in application code.
    op.add_column('approval_queue', sa.Column('short_code', sa.String(length=16), nullable=True))
    op.execute(
        "UPDATE approval_queue SET short_code = 'D' || lpad(nextval('approval_queue_short_code_seq')::text, 4, '0') "
        "WHERE short_code IS NULL"
    )
    op.alter_column('approval_queue', 'short_code', nullable=False)
    op.create_unique_constraint(op.f('uq_approval_queue_short_code'), 'approval_queue', ['short_code'])
    op.alter_column(
        'approval_queue', 'short_code',
        server_default=sa.text("'D' || lpad(nextval('approval_queue_short_code_seq')::text, 4, '0')"),
    )

    op.add_column('approval_queue', sa.Column('initial_notice_sent_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('approval_queue', 'initial_notice_sent_at')
    op.drop_constraint(op.f('uq_approval_queue_short_code'), 'approval_queue', type_='unique')
    op.drop_column('approval_queue', 'short_code')
    op.execute("DROP SEQUENCE IF EXISTS approval_queue_short_code_seq")
