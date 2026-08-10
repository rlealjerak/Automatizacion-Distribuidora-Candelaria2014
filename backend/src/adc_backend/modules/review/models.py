"""
Review module model: the manual review queue.

Per CLAUDE.md, several cases route here instead of an automatic
classification: low-confidence matches, display/bundle SKUs, tiered promo
pricing blocks, gated-but-not-yet-approved items with a notably strong
deal, and any genuinely ambiguous case. One entry per raw_line_item.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from adc_backend.db.base import Base


class ReviewReason(str, enum.Enum):
    LOW_CONFIDENCE_MATCH = "low_confidence_match"
    DISPLAY_BUNDLE_SKU = "display_bundle_sku"
    TIERED_PROMO_PRICING = "tiered_promo_pricing"
    GATED_PENDING_STRONG_DEAL = "gated_pending_strong_deal"
    AMBIGUOUS_PARSE = "ambiguous_parse"
    OTHER = "other"


class ReviewStatus(str, enum.Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ManualReviewQueue(Base):
    __tablename__ = "manual_review_queue"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    list_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("list_runs.id"), nullable=False)
    raw_line_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("raw_line_items.id"), nullable=False, unique=True
    )

    reason: Mapped[ReviewReason] = mapped_column(Enum(ReviewReason, name="review_reason"), nullable=False)
    reason_notes: Mapped[str | None] = mapped_column(Text)

    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status"), default=ReviewStatus.PENDING
    )
    resolution_notes: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None]
    resolved_at: Mapped[datetime | None]

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    raw_line_item = relationship("RawLineItem")
