"""upc-first matching: raw_line_items.upc, product_matches.candidate_asins, match_source/review_reason enum additions

Revision ID: c3f7a9d1e52b
Revises: 8f3b1c2d4e67
Create Date: 2026-10-11 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c3f7a9d1e52b'
down_revision: Union[str, None] = '8f3b1c2d4e67'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('raw_line_items', sa.Column('upc', sa.String(), nullable=True))
    op.add_column('product_matches', sa.Column('candidate_asins', postgresql.JSONB(), nullable=True))

    # Same pattern as e64eb0eeb98c - autogenerate doesn't detect new
    # members on an existing Postgres native ENUM type. Safe inside this
    # transaction on Postgres 16 (this project's version) as long as the
    # new value isn't used in the same transaction, which it isn't here.
    #
    # UPPERCASE, matching the member *name*, not .value - confirmed by a
    # real local-Postgres insert failure (2026-10-11): SQLAlchemy's
    # native Enum type sends the Python enum member's name for a
    # str-Enum class here (e.g. "UPC"), not "upc" - every existing label
    # in both these columns is uppercase for the same reason (see
    # 4028e416c123_initial_schema.py).
    op.execute("ALTER TYPE match_source ADD VALUE IF NOT EXISTS 'UPC'")
    op.execute("ALTER TYPE review_reason ADD VALUE IF NOT EXISTS 'MULTIPLE_UPC_MATCHES'")

    # e64eb0eeb98c made the exact same casing mistake adding
    # 'ambiguous_restriction' (lowercase) - never caught because that
    # path had never been exercised against a real DB insert until this
    # session's 20-row MTZ re-run hit a genuine SP-API NOT_ELIGIBLE
    # restriction on a real ASIN. Fixed here (the lowercase value is
    # left in place - no DROP VALUE in Postgres - but now unreachable
    # since Python code only ever sends the uppercase name).
    op.execute("ALTER TYPE review_reason ADD VALUE IF NOT EXISTS 'AMBIGUOUS_RESTRICTION'")


def downgrade() -> None:
    op.drop_column('product_matches', 'candidate_asins')
    op.drop_column('raw_line_items', 'upc')

    # Postgres has no DROP VALUE for enum types - removing 'upc'/
    # 'multiple_upc_matches' would require rebuilding both types, not
    # worth doing for a downgrade path. Same known no-op category as
    # e64eb0eeb98c's downgrade.
