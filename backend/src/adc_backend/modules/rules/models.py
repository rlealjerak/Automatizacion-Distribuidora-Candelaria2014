"""
Rules module models: configurable business rules and the classification
output (with reasoning trace) they produce per line item per run.

Per CLAUDE.md, rules must be adjustable by a non-technical owner without a
code change - `BusinessRulesConfig.config` is the structured (JSON)
representation of the ROI/margin/velocity/etc. table in CLAUDE.md, with
one row active at a time. `ClassificationResult.rule_trace` exists so the
owner can always see *why* a classification was reached, not just the
label - never store the label without it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Enum, ForeignKey, Index, Numeric, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from adc_backend.db.base import Base


class ClassificationLabel(str, enum.Enum):
    BUY = "buy"
    REVIEW = "review"
    NEGOTIATE = "negotiate"
    NO_BUY = "no_buy"
    HIGH_RISK = "high_risk"


class BusinessRulesConfig(Base):
    """
    A versioned snapshot of rule thresholds. Exactly one row should have
    is_active=true at a time (enforced by the partial unique index below);
    changing rules means inserting a new row and flipping the flag, so old
    runs stay traceable to the config that actually produced them.
    """

    __tablename__ = "business_rules_configs"
    __table_args__ = (
        Index(
            "uq_business_rules_configs_one_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str | None]  # e.g. "default", "q4-aggressive"

    # Structured config: roi_threshold_pct, margin_target_pct,
    # margin_exception_table, velocity_threshold_units_per_month,
    # seasonality settings, etc. Shape owned by the rule engine (step 8),
    # not fixed at the schema level.
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)

    is_active: Mapped[bool] = mapped_column(default=False, server_default="false")
    notes: Mapped[str | None]
    created_by: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    classification_results = relationship("ClassificationResult", back_populates="rules_config")


class ClassificationResult(Base):
    __tablename__ = "classification_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    list_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("list_runs.id"), nullable=False)
    raw_line_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("raw_line_items.id"), nullable=False, unique=True
    )
    amazon_data_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("amazon_data_snapshots.id"))
    rules_config_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("business_rules_configs.id"))

    classification: Mapped[ClassificationLabel] = mapped_column(
        Enum(ClassificationLabel, name="classification_label"), nullable=False
    )
    roi: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    margin: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))

    # Ordered list of {rule_name, inputs, threshold, result, reasoning} -
    # the full "why" behind the label. Required, not optional: CLAUDE.md is
    # explicit that the owner must always see the reasoning, not just the
    # label.
    rule_trace: Mapped[list] = mapped_column(JSONB, nullable=False)

    computed_at: Mapped[datetime] = mapped_column(server_default=func.now())

    rules_config = relationship("BusinessRulesConfig", back_populates="classification_results")
