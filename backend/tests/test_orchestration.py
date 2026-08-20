"""
Orchestration tests: verifies the pipeline's control flow (matching ->
pricing/fees/restrictions -> Keepa -> classification -> review routing)
and, critically, per-row error isolation - an explicit MVP acceptance
criterion (CLAUDE.md: "process >=5,000 rows... one bad row must not fail
the whole run"). Uses stub SP-API/Keepa clients (see matching engine
tests for the same pattern) - this validates orchestration.py's own
logic, not live API behavior (see sp_api_client.py/keepa_client.py
module docstrings on that caveat).
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest

from adc_backend.modules.amazon.sp_api_client import (
    CatalogMatch,
    FeesEstimate,
    PricingSnapshot,
    RestrictionsResult,
)

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


@pytest.fixture
def db_session():
    from adc_backend.db import models  # noqa: F401
    from adc_backend.db.base import get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class _StubSPAPIClient:
    """Good pricing/fees/restrictions for everything, except a matching call that fails for one designated item number."""

    def __init__(self, fail_for_item_number: str | None = None):
        self._fail_for = fail_for_item_number

    def search_catalog_items(self, keywords: str, marketplace_id: str = "", brand: str | None = None):
        if self._fail_for and self._fail_for in keywords:
            raise RuntimeError(f"Simulated SP-API failure for {keywords!r}")
        return [CatalogMatch(asin=f"B0{abs(hash(keywords)) % 10**8:08d}", title=keywords, brand=brand)]

    def get_pricing(self, asin: str, marketplace_id: str = ""):
        return PricingSnapshot(
            asin=asin,
            current_price=Decimal("20.00"),
            buy_box_price=Decimal("20.00"),
            buy_box_owner="A1SELLER",
            amazon_has_buy_box=False,
            seller_count=2,
            raw_response={},
        )

    def get_fees_estimate(self, asin: str, price: Decimal, marketplace_id: str = ""):
        return FeesEstimate(asin=asin, referral_fee=Decimal("3.00"), fba_fee=Decimal("2.00"), other_fees={}, raw_response={})

    def get_listing_restrictions(self, asin: str, seller_id: str, marketplace_id: str = ""):
        return RestrictionsResult(
            asin=asin, is_restricted=False, is_gated=False, ambiguous_restriction=False, approved_for_seller=True, raw_response={}
        )


class _StubKeepaClient:
    def get_product(self, asin: str, stats_days: int = 90):
        from adc_backend.modules.amazon.keepa_client import KeepaSnapshot

        return KeepaSnapshot(
            asin=asin,
            current_sales_rank=5000,
            avg_sales_rank_30d=5200,
            sales_rank_drops_30d=100,
            sales_rank_drops_90d=250,
            current_price=Decimal("20.00"),
            avg_price_90d=Decimal("21.00"),
            out_of_stock_percentage_90d=1,
            raw_response={},
        )


def _setup_run_with_items(db_session, item_specs: list[dict]):
    from adc_backend.db.core_models import ListRun, SourceFileType, Supplier
    from adc_backend.modules.ingestion.models import RawLineItem
    from adc_backend.modules.rules.config import seed_default_rules_config

    seed_default_rules_config(db_session)

    supplier = Supplier(name=f"Test Supplier {uuid.uuid4()}", code=f"test-{uuid.uuid4().hex[:8]}")
    db_session.add(supplier)
    db_session.flush()
    run = ListRun(
        supplier_id=supplier.id,
        source_file_s3_key="s3://x/y.csv",
        source_file_original_filename="y.csv",
        source_file_type=SourceFileType.CSV,
        confirmed_column_mapping={
            "supplier_item_number": "ITEM #",
            "description": "DESC",
            "brand": None,
            "unit_price": "PRICE",
            "case_quantity": None,
            "case_unit": None,
        },
    )
    db_session.add(run)
    db_session.flush()

    for i, spec in enumerate(item_specs, start=1):
        db_session.add(
            RawLineItem(
                list_run_id=run.id,
                row_number=i,
                raw_data={"ITEM #": spec["item_number"], "DESC": spec["description"], "PRICE": spec["price"]},
            )
        )
    db_session.flush()
    return run


def test_all_rows_succeed(db_session):
    from adc_backend.modules.tools.orchestration import process_run

    run = _setup_run_with_items(
        db_session,
        [
            {"item_number": "ABC-1", "description": "Blue Widget", "price": "10.00"},
            {"item_number": "ABC-2", "description": "Red Gadget", "price": "10.00"},
        ],
    )

    summary = process_run(db_session, run.id, _StubSPAPIClient(), _StubKeepaClient())

    assert summary["total_rows"] == 2
    assert summary["processed_rows"] == 2
    assert summary["error_rows"] == 0

    from adc_backend.db.core_models import ListRunStatus

    db_session.refresh(run)
    assert run.status == ListRunStatus.COMPLETED


def test_one_bad_row_does_not_fail_the_whole_run(db_session):
    """The core acceptance criterion: a bad row is isolated, the run still completes."""
    from adc_backend.modules.tools.orchestration import process_run

    run = _setup_run_with_items(
        db_session,
        [
            {"item_number": "ABC-1", "description": "Blue Widget", "price": "10.00"},
            {"item_number": "BAD-ITEM", "description": "Blue Widget", "price": "10.00"},  # triggers the stub's simulated failure
            {"item_number": "ABC-3", "description": "Green Gizmo", "price": "10.00"},
        ],
    )

    summary = process_run(db_session, run.id, _StubSPAPIClient(fail_for_item_number="Blue Widget"), _StubKeepaClient())

    # Both rows describing "Blue Widget" hit the simulated failure (the stub
    # keys off keywords, matching real behavior where the same bad input
    # fails the same way) - only "Green Gizmo" succeeds cleanly.
    assert summary["total_rows"] == 3
    assert summary["error_rows"] == 2
    assert summary["processed_rows"] == 1

    from adc_backend.db.core_models import ListRunStatus
    from adc_backend.modules.ingestion.models import ParseStatus, RawLineItem

    db_session.refresh(run)
    assert run.status == ListRunStatus.PARTIAL  # not FAILED - the run itself completed despite row errors

    items = {i.supplier_item_number: i for i in db_session.query(RawLineItem).filter_by(list_run_id=run.id).all()}
    assert items["BAD-ITEM"].parse_status == ParseStatus.ERROR
    assert items["ABC-3"].parse_status == ParseStatus.OK


def test_matched_item_gets_full_pipeline_result(db_session):
    from adc_backend.modules.rules.models import ClassificationLabel, ClassificationResult
    from adc_backend.modules.tools.orchestration import process_run

    run = _setup_run_with_items(db_session, [{"item_number": "ABC-1", "description": "Blue Widget", "price": "10.00"}])
    process_run(db_session, run.id, _StubSPAPIClient(), _StubKeepaClient())

    result = db_session.query(ClassificationResult).filter_by(list_run_id=run.id).one()
    # cost 10, sell 20, fees 5 -> profit 5 -> ROI 50%, margin 25% - clean BUY
    assert result.classification == ClassificationLabel.BUY
    assert result.roi == Decimal(50)


def test_bundle_item_skips_matching_and_goes_straight_to_review(db_session):
    from adc_backend.db.core_models import ListRunStatus
    from adc_backend.modules.review.models import ManualReviewQueue, ReviewReason
    from adc_backend.modules.tools.orchestration import process_run

    run = _setup_run_with_items(db_session, [{"item_number": "DISP-1", "description": "9 COLOR DISPLAY", "price": "45.00"}])
    summary = process_run(db_session, run.id, _StubSPAPIClient(), _StubKeepaClient())

    assert summary["error_rows"] == 0  # a bundle item isn't an error - it's expected routing, not a failure
    assert summary["review_queue_rows"] == 1

    entry = db_session.query(ManualReviewQueue).filter_by(list_run_id=run.id).one()
    assert entry.reason == ReviewReason.DISPLAY_BUNDLE_SKU

    db_session.refresh(run)
    assert run.status == ListRunStatus.COMPLETED
