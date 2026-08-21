"""
Full per-run pipeline: normalize -> match -> pull live Amazon data ->
classify -> route to review. This is what the SQS worker (worker.py, its
own ECS service) runs for one list_run, dequeued from the
list-processing queue that POST /runs/{run_id}/process enqueues to.

Per-row error isolation is an explicit MVP acceptance criterion
(CLAUDE.md: "one bad row must not fail the whole run") - every row is
wrapped individually; a failure anywhere in matching/pricing/fees/
restrictions/Keepa/classification for one row increments the run's
error_rows count and moves on, it never aborts the run.

*** The SP-API/Keepa calls inside this pipeline are unverified against
live data *** (see sp_api_client.py / keepa_client.py module docstrings)
- this orchestration logic itself (the control flow, error isolation,
per-row sequencing) is what's actually tested here, via stub clients, not
a claim that a full live run has ever been executed.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from adc_backend.config import get_settings
from adc_backend.db.core_models import ListRun, ListRunStatus
from adc_backend.modules.amazon.models import AmazonDataSnapshot, GatedApprovalStatus
from adc_backend.modules.ingestion.models import LineItemType, ParseStatus, RawLineItem
from adc_backend.modules.matching.engine import find_or_propose_match
from adc_backend.modules.normalization.service import apply_normalization
from adc_backend.modules.review.service import evaluate_and_route
from adc_backend.modules.rules.service import RulesServiceError, classify_line_item

logger = logging.getLogger(__name__)


class OrchestrationError(Exception):
    """Run-level failure - couldn't even start (missing mapping, no active rules config, etc.)."""


def process_run(db: Session, run_id: uuid.UUID, sp_api_client, keepa_client) -> dict:
    run = db.get(ListRun, run_id)
    if run is None:
        raise OrchestrationError(f"No list_run with id {run_id}")
    if run.confirmed_column_mapping is None:
        raise OrchestrationError(f"list_run {run_id} has no confirmed column mapping - can't process")

    apply_normalization(db, run_id)

    items = db.execute(select(RawLineItem).where(RawLineItem.list_run_id == run_id)).scalars().all()

    error_count = 0
    review_count = 0
    processed_count = 0

    for item in items:
        try:
            _process_one_item(db, run, item, sp_api_client, keepa_client)
            processed_count += 1
        except Exception:
            # Deliberately broad: a bad row (weird data, an unexpected API
            # shape) must never take down the whole run. Logged with full
            # context for debugging; the row itself gets flagged for
            # review rather than silently disappearing.
            logger.exception("Row processing failed for raw_line_item %s in run %s", item.id, run_id)
            item.parse_status = ParseStatus.ERROR
            item.parse_notes = (item.parse_notes or "") + " | Processing failed - see server logs."
            error_count += 1
        finally:
            review_entry = evaluate_and_route(db, run_id, item.id)
            if review_entry is not None:
                review_count += 1

    run.processed_rows = processed_count
    run.error_rows = error_count
    run.status = ListRunStatus.PARTIAL if error_count > 0 else ListRunStatus.COMPLETED
    db.flush()

    return {
        "total_rows": len(items),
        "processed_rows": processed_count,
        "error_rows": error_count,
        "review_queue_rows": review_count,
    }


def _process_one_item(db: Session, run: ListRun, item: RawLineItem, sp_api_client, keepa_client) -> None:
    # Bundle/promo/ambiguous items never reach matching/pricing/classification
    # at all - they go straight to the review queue (handled by the caller's
    # evaluate_and_route call after this function returns).
    if item.item_type != LineItemType.STANDARD or item.parse_status != ParseStatus.OK:
        return
    if not item.supplier_item_number:
        return

    match = find_or_propose_match(
        db,
        sp_api_client,
        supplier_id=run.supplier_id,
        supplier_item_number=item.supplier_item_number,
        description=item.description or "",
        brand=item.brand,
    )
    item.product_match_id = match.id
    db.flush()

    if not match.asin:
        return  # no confident ASIN - stays unmatched, review queue picks this up on the low-confidence-match path

    _fetch_and_store_snapshot(db, run, item, match.asin, sp_api_client, keepa_client)
    try:
        classify_line_item(db, run.id, item.id)
    except RulesServiceError as e:
        # No active rules config, or no snapshot after all - shouldn't
        # normally happen given the guard above, but surfaces as a review
        # case rather than crashing the row.
        logger.warning("Classification skipped for raw_line_item %s: %s", item.id, e)


def _fetch_and_store_snapshot(db: Session, run: ListRun, item: RawLineItem, asin: str, sp_api_client, keepa_client) -> None:
    settings = get_settings()

    pricing = sp_api_client.get_pricing(asin)
    sell_price = pricing.buy_box_price or pricing.current_price or Decimal(0)
    fees = sp_api_client.get_fees_estimate(asin, sell_price)
    restrictions = sp_api_client.get_listing_restrictions(asin, seller_id=settings.sp_api_seller_id)

    keepa_snapshot = None
    try:
        keepa_snapshot = keepa_client.get_product(asin)
    except Exception:  # noqa: BLE001 - intentionally broad: Keepa is a secondary signal (velocity)
        # its absence shouldn't block pricing/fees/restrictions data, which is enough to classify.
        logger.warning("Keepa lookup failed for ASIN %s - proceeding without velocity data", asin)

    gated_status = GatedApprovalStatus.NOT_APPLICABLE
    if restrictions.is_gated:
        gated_status = GatedApprovalStatus.APPROVED if restrictions.approved_for_seller else GatedApprovalStatus.NOT_APPROVED

    snapshot = AmazonDataSnapshot(
        list_run_id=run.id,
        raw_line_item_id=item.id,
        asin=asin,
        current_price=pricing.current_price,
        buy_box_price=pricing.buy_box_price,
        buy_box_owner=pricing.buy_box_owner,
        amazon_has_buy_box=pricing.amazon_has_buy_box,
        referral_fee=fees.referral_fee,
        fba_fee=fees.fba_fee,
        other_fees=fees.other_fees,
        seller_count=pricing.seller_count,
        is_restricted=restrictions.is_restricted,
        ambiguous_restriction=restrictions.ambiguous_restriction,
        is_gated=restrictions.is_gated,
        gated_approval_status=gated_status,
        sales_rank=keepa_snapshot.current_sales_rank if keepa_snapshot else None,
        sales_rank_drops_30d=keepa_snapshot.sales_rank_drops_30d if keepa_snapshot else None,
        sales_rank_drops_90d=keepa_snapshot.sales_rank_drops_90d if keepa_snapshot else None,
        sp_api_raw_response={"pricing": pricing.raw_response, "fees": fees.raw_response, "restrictions": restrictions.raw_response},
        keepa_raw_response=keepa_snapshot.raw_response if keepa_snapshot else None,
    )
    db.add(snapshot)
    db.flush()
