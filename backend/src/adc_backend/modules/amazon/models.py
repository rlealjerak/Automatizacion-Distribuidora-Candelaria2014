"""
Amazon module model: a snapshot of live SP-API/Keepa data.

Per CLAUDE.md, live data is fetched fresh from SP-API/Keepa on every run
and never cached as a static assumption - so this is one row per
(list_run, raw_line_item), not something upserted/reused across runs.
Historical snapshots stay queryable for run-to-run comparison.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, Enum, ForeignKey, Numeric, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from adc_backend.db.base import Base


class GatedApprovalStatus(str, enum.Enum):
    NOT_APPLICABLE = "not_applicable"  # not a gated category/product
    APPROVED = "approved"  # already approved for this seller account
    PENDING_APPROVAL = "pending_approval"  # owner has submitted an invoice, awaiting Amazon
    NOT_APPROVED = "not_approved"  # gated, no approval in progress yet


class AmazonDataSnapshot(Base):
    __tablename__ = "amazon_data_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    list_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("list_runs.id"), nullable=False)
    raw_line_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_line_items.id"), nullable=False)
    asin: Mapped[str] = mapped_column(nullable=False)

    fetched_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    # Pricing / fees (SP-API)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    buy_box_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    buy_box_owner: Mapped[str | None]  # seller name/id, or "AMAZON"
    amazon_has_buy_box: Mapped[bool | None] = mapped_column(Boolean)
    referral_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    fba_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    other_fees: Mapped[dict | None] = mapped_column(JSONB)  # breakdown of any remaining fee components

    # Sales / rank (Keepa)
    estimated_monthly_sales: Mapped[int | None]
    sales_rank: Mapped[int | None]
    sales_rank_category: Mapped[str | None]

    # Risk factors
    seller_count: Mapped[int | None]
    is_restricted: Mapped[bool | None] = mapped_column(Boolean)
    is_gated: Mapped[bool | None] = mapped_column(Boolean)
    gated_approval_status: Mapped[GatedApprovalStatus] = mapped_column(
        Enum(GatedApprovalStatus, name="gated_approval_status"), default=GatedApprovalStatus.NOT_APPLICABLE
    )
    manufacturer_sells_directly: Mapped[bool | None] = mapped_column(Boolean)

    # Full raw responses kept for audit/debugging - never re-derived from,
    # always re-fetched live on the next run.
    sp_api_raw_response: Mapped[dict | None] = mapped_column(JSONB)
    keepa_raw_response: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    raw_line_item = relationship("RawLineItem")
