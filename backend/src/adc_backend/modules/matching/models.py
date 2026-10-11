"""
Matching module model: the persistent supplier_item_number -> ASIN
mapping.

Per CLAUDE.md: once the owner confirms a match, it must be saved
permanently and reused instantly (at high confidence) on future lists
from that supplier - this is what shrinks review workload over time. That
means one row per (supplier_id, supplier_item_number), updated in place -
never one row per run.

CLAUDE.md's "Data reality" section originally said no supplier ever
provides UPC/EAN - the real MTZ price list (2026-10-11) disproved that
for at least one real supplier. UPC is still deliberately NOT the join
key here (supplier_item_number is, per the persistent-mapping design
above) - it's an independent, higher-confidence path to an ASIN, tried
first when present and valid (see modules/matching/engine.py and
modules/matching/upc.py), falling back to text/brand matching otherwise.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Enum, ForeignKey, Numeric, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from adc_backend.db.base import Base


class MatchStatus(str, enum.Enum):
    PROPOSED = "proposed"  # matching engine suggested this; not yet reviewed by owner
    CONFIRMED = "confirmed"  # owner confirmed - safe to auto-apply on future runs at high confidence
    REJECTED = "rejected"  # owner rejected this suggestion; kept so it isn't re-proposed identically


class MatchSource(str, enum.Enum):
    AUTO = "auto"  # produced by the text/brand matching engine
    MANUAL = "manual"  # owner picked/entered the ASIN directly
    UPC = "upc"  # produced by SP-API's identifier (UPC/EAN) lookup, added 2026-10-11


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

    # Populated only when a UPC/EAN identifier lookup returns more than
    # one ASIN for the same barcode (added 2026-10-11) - asin stays null
    # in that case (no single confident pick), and this holds every
    # candidate so the review queue can show them all rather than one
    # arbitrarily chosen guess.
    candidate_asins: Mapped[list[str] | None] = mapped_column(JSONB)

    match_status: Mapped[MatchStatus] = mapped_column(
        Enum(MatchStatus, name="match_status"), default=MatchStatus.PROPOSED
    )
    match_source: Mapped[MatchSource] = mapped_column(Enum(MatchSource, name="match_source"))

    confirmed_by: Mapped[str | None]
    confirmed_at: Mapped[datetime | None]

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    raw_line_items = relationship("RawLineItem", back_populates="product_match")
