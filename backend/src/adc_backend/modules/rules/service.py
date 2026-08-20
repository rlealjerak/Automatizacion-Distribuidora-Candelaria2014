"""Rule engine orchestration: load the active config, build inputs from a snapshot, persist the result."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from adc_backend.modules.amazon.models import AmazonDataSnapshot
from adc_backend.modules.ingestion.models import RawLineItem
from adc_backend.modules.rules.engine import ClassificationInputs, classify
from adc_backend.modules.rules.models import BusinessRulesConfig, ClassificationResult


class RulesServiceError(Exception):
    pass


def get_active_config(db: Session) -> BusinessRulesConfig:
    config = db.execute(select(BusinessRulesConfig).where(BusinessRulesConfig.is_active.is_(True))).scalar_one_or_none()
    if config is None:
        raise RulesServiceError("No active business_rules_config - run seed_default_rules_config() first")
    return config


def classify_line_item(db: Session, list_run_id: uuid.UUID, raw_line_item_id: uuid.UUID) -> ClassificationResult:
    line_item = db.get(RawLineItem, raw_line_item_id)
    if line_item is None:
        raise RulesServiceError(f"No raw_line_item with id {raw_line_item_id}")

    snapshot = db.execute(
        select(AmazonDataSnapshot)
        .where(AmazonDataSnapshot.raw_line_item_id == raw_line_item_id, AmazonDataSnapshot.list_run_id == list_run_id)
        .order_by(AmazonDataSnapshot.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if snapshot is None:
        raise RulesServiceError(
            f"No amazon_data_snapshot for raw_line_item {raw_line_item_id} in run {list_run_id} - "
            "fetch live SP-API/Keepa data before classifying"
        )

    rules_config = get_active_config(db)

    other_fees_total = Decimal(0)
    for value in (snapshot.other_fees or {}).values():
        if value is not None:
            other_fees_total += Decimal(str(value))

    inputs = ClassificationInputs(
        unit_cost=line_item.unit_price,
        sell_price=snapshot.buy_box_price or snapshot.current_price,
        referral_fee=snapshot.referral_fee,
        fba_fee=snapshot.fba_fee,
        other_fees_total=other_fees_total,
        is_restricted=bool(snapshot.is_restricted),
        ambiguous_restriction=bool(snapshot.ambiguous_restriction),
        is_gated=bool(snapshot.is_gated),
        gated_approval_status=snapshot.gated_approval_status.value,
        manufacturer_sells_directly=bool(snapshot.manufacturer_sells_directly),
        amazon_has_buy_box=snapshot.amazon_has_buy_box,
        seller_count=snapshot.seller_count,
        sales_rank_drops_30d=snapshot.sales_rank_drops_30d,
    )

    decision = classify(inputs, rules_config.config)

    existing = db.execute(
        select(ClassificationResult).where(ClassificationResult.raw_line_item_id == raw_line_item_id)
    ).scalar_one_or_none()

    if existing is not None:
        existing.amazon_data_snapshot_id = snapshot.id
        existing.rules_config_id = rules_config.id
        existing.classification = decision.classification
        existing.roi = decision.roi_pct
        existing.margin = decision.margin_pct
        existing.rule_trace = decision.rule_trace
        result = existing
    else:
        result = ClassificationResult(
            list_run_id=list_run_id,
            raw_line_item_id=raw_line_item_id,
            amazon_data_snapshot_id=snapshot.id,
            rules_config_id=rules_config.id,
            classification=decision.classification,
            roi=decision.roi_pct,
            margin=decision.margin_pct,
            rule_trace=decision.rule_trace,
        )
        db.add(result)

    db.flush()
    return result
