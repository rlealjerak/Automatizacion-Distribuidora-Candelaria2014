"""add approval_audit_log

Revision ID: 0b21449f1923
Revises: a1c9f2e7d834
Create Date: 2026-10-08 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0b21449f1923'
down_revision: Union[str, None] = 'a1c9f2e7d834'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'approval_audit_log',
        sa.Column('id', sa.UUID(), nullable=False),
        # No FK, deliberately nullable - a rejected call against a
        # bad/unknown id still gets logged here (see models.py docstring).
        sa.Column('approval_id', sa.UUID(), nullable=True),
        sa.Column('requested_approval_id', sa.Text(), nullable=True),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column(
            'event',
            sa.Enum('APPROVED', 'DENIED', 'REVOKED', 'REJECTED', name='approval_audit_event'),
            nullable=False,
        ),
        sa.Column('ok', sa.Boolean(), nullable=False),
        sa.Column('actor', sa.Text(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_approval_audit_log')),
    )
    op.create_index(op.f('ix_approval_audit_log_approval_id'), 'approval_audit_log', ['approval_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_approval_audit_log_approval_id'), table_name='approval_audit_log')
    op.drop_table('approval_audit_log')

    # Postgres native ENUM types aren't dropped by op.drop_table() - same
    # documented quirk as every other migration here (see
    # docs/decisions/0002-database-schema-decisions.md).
    op.execute('DROP TYPE IF EXISTS approval_audit_event')
