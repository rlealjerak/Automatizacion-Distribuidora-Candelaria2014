"""add approval_queue

Revision ID: a1c9f2e7d834
Revises: e64eb0eeb98c
Create Date: 2026-08-28 09:15:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1c9f2e7d834'
down_revision: Union[str, None] = 'e64eb0eeb98c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'approval_queue',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('analysis_result_ref', sa.UUID(), nullable=False),
        sa.Column(
            'type',
            sa.Enum('PURCHASE_RECOMMENDATION', name='approval_item_type'),
            nullable=False,
        ),
        sa.Column(
            'status',
            sa.Enum(
                'QUEUED', 'PENDING', 'REMINDER_1', 'REMINDER_2', 'REMINDER_3',
                'REMINDER_4', 'REMINDER_5', 'REMINDER_6', 'HOLDING', 'APPROVED', 'DENIED',
                name='approval_status',
            ),
            nullable=False,
        ),
        sa.Column('queue_position', sa.Integer(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('summary_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # timezone=True on the four timestamp columns below is deliberate,
        # not the pattern the rest of this schema uses - see the matching
        # note in modules/approvals/models.py for why.
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('pending_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_reminder_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reminder_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolution', sa.Enum('APPROVED', 'DENIED', name='approval_resolution'), nullable=True),
        sa.Column('resolution_note', sa.Text(), nullable=True),
        sa.Column('is_active_item', sa.Boolean(), server_default='false', nullable=False),
        sa.ForeignKeyConstraint(
            ['analysis_result_ref'], ['classification_results.id'], name=op.f('fk_approval_queue_analysis_result_ref')
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_approval_queue')),
    )
    op.create_index(
        'uq_approval_queue_one_active_item',
        'approval_queue',
        ['is_active_item'],
        unique=True,
        postgresql_where=sa.text('is_active_item'),
    )


def downgrade() -> None:
    op.drop_index('uq_approval_queue_one_active_item', table_name='approval_queue', postgresql_where=sa.text('is_active_item'))
    op.drop_table('approval_queue')

    # Postgres native ENUM types aren't dropped by op.drop_table() - same
    # documented quirk as every other migration here (see
    # docs/decisions/0002-database-schema-decisions.md and the
    # e64eb0eeb98c migration's identical note).
    op.execute('DROP TYPE IF EXISTS approval_item_type')
    op.execute('DROP TYPE IF EXISTS approval_status')
    op.execute('DROP TYPE IF EXISTS approval_resolution')
