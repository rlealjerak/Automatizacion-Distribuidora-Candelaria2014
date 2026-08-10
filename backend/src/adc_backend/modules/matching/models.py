"""
Matching module model: the persistent supplier_item_number -> ASIN
mapping.

Per CLAUDE.md: once the owner confirms a match, it must be saved
permanently and reused instantly (at high confidence) on future lists
from that supplier - this is what shrinks review workload over time. That
means one row per (supplier_id, supplier_item_number), updated in place -
never one row per run.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Enum, ForeignKey, Numeric, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from adc_backend.db.base import Base


class MatchStatus(str, enum.Enum):
    PROPOSED = "proposed"  # matching engine suggested this; not yet reviewed by owner
    CONFIRMED = "confirmed"  # owner confirmed - safe to auto-apply on future runs at high confidence
    REJECTED = "rejected"  # owner rejected this suggestion; kept so it isn't re-proposed identically


class MatchSource(str, enum.Enum):
    AUTO = "auto"  # produced by the text/brand matching engine
    MANUAL = "manual"  # owner picked/entered the ASIN directly


class ProductMatch(Base):
    __tablename__ = "product_matches"
    __table_args__ = (
        UniqueConstraint("supplier_id", "supplier_item_number", name="uq_product_matches_supplier_item"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    supplier_item_number: Mapped[str] = mapped_column(nullable=False)

    asin: Mapped[str | None]  # nullable so "considered, no confident match" is representable
    match_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))  # 0.0000-1.0000

    match_status: Mapped[MatchStatus] = mapped_column(
        Enum(MatchStatus, name="match_status"), default=MatchStatus.PROPOSED
    )
    match_source: Mapped[MatchSource] = mapped_column(Enum(MatchSource, name="match_source"))

    confirmed_by: Mapped[str | None]
    confirmed_at: Mapped[datetime | None]

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    raw_line_items = relationship("RawLineItem", back_populates="product_match")
