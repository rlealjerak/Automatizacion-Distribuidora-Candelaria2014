"""
Ingestion module models: one row per parsed line item from a supplier
list, plus the case-unit lookup table normalization depends on.

See CLAUDE.md "Data reality" section - supplier files are messy (PDF
catalogs, inconsistent headers, non-standard units like DP/ST, display-
bundle SKUs, tiered promo pricing blocks). `raw_data` always preserves the
row exactly as parsed; the normalized columns below are best-effort and
may be null/flagged when parsing can't confidently fill them in.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Enum, ForeignKey, Numeric, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from adc_backend.db.base import Base


class LineItemType(str, enum.Enum):
    STANDARD = "standard"  # normal per-unit line item, eligible for regular ASIN matching
    DISPLAY_BUNDLE = "display_bundle"  # kit/display SKU (e.g. "9 COLOR DISPLAY") - manual review, no per-unit match
    TIERED_PROMO = "tiered_promo"  # tiered promotional pricing block - manual review, not auto-parsed
    AMBIGUOUS = "ambiguous"  # parser couldn't confidently classify - manual review


class ParseStatus(str, enum.Enum):
    OK = "ok"
    NEEDS_REVIEW = "needs_review"
    ERROR = "error"


class RawLineItem(Base):
    """One line item from one supplier list, for one run."""

    __tablename__ = "raw_line_items"
    __table_args__ = (UniqueConstraint("list_run_id", "row_number", name="uq_raw_line_items_run_row"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    list_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("list_runs.id"), nullable=False)
    row_number: Mapped[int] = mapped_column(nullable=False)  # 1-based position in source file

    # Exactly as parsed, keyed by original source headers - never mutated.
    # This is what makes a bad normalization step debuggable/re-runnable.
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Normalized fields - best-effort, per confirmed column mapping.
    supplier_item_number: Mapped[str | None]  # join key for the persistent ASIN mapping
    description: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None]
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    case_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    case_unit_raw: Mapped[str | None]  # as given by supplier: EA, DP, CA, ST, PCS, ...
    case_unit_normalized: Mapped[str | None]  # resolved via unit_lookup, null if unresolved

    item_type: Mapped[LineItemType] = mapped_column(
        Enum(LineItemType, name="line_item_type"), default=LineItemType.STANDARD
    )
    parse_status: Mapped[ParseStatus] = mapped_column(
        Enum(ParseStatus, name="parse_status"), default=ParseStatus.OK
    )
    parse_notes: Mapped[str | None] = mapped_column(Text)  # why flagged, parser warnings, etc.

    # Set once the matching engine (or a persisted prior mapping) resolves
    # this item to an ASIN. Nullable - many items sit unmatched/in-review.
    product_match_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("product_matches.id"))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    list_run = relationship("ListRun", back_populates="raw_line_items")
    product_match = relationship("ProductMatch", back_populates="raw_line_items")


class UnitLookup(Base):
    """
    Case-unit code lookup (e.g. DP -> display pack, ST -> set).

    Structure only for now - seeding real values and wiring it into
    normalization is build-order step 4. `units_per_case` is nullable and
    `multiplier_confirmed` defaults false because, per CLAUDE.md, these
    codes are often supplier-specific and not safe to assume a fixed
    multiplier for without confirmation.
    """

    __tablename__ = "unit_lookup"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    raw_unit: Mapped[str] = mapped_column(nullable=False, unique=True)
    normalized_unit: Mapped[str] = mapped_column(nullable=False)
    units_per_case: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    multiplier_confirmed: Mapped[bool] = mapped_column(default=False, server_default="false")
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
