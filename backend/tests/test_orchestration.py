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


# --- added 2026-11-14: savepoint isolation, lazy Keepa, checkpoint/resume, single-item reprocess ---


class _DBPoisoningSPAPIClient(_StubSPAPIClient):
    """Issues a genuinely failing SQL statement mid-row for one ASIN-producing description, aborting the transaction."""

    def __init__(self, db_session, poison_description: str):
        super().__init__()
        self._db = db_session
        self._poison = poison_description

    def search_catalog_items(self, keywords: str, marketplace_id: str = "", brand: str | None = None):
        if self._poison in keywords:
            from sqlalchemy import text

            self._db.execute(text("SELECT * FROM definitely_not_a_real_table"))
        return super().search_catalog_items(keywords, marketplace_id, brand)


def test_database_error_in_one_row_does_not_abort_the_rest(db_session):
    # Codex review on PR #14: catching the exception without a savepoint
    # left Postgres' transaction aborted, so every following row (and the
    # final flush) failed with PendingRollbackError. With per-row
    # savepoints only the poisoned row is lost.
    from adc_backend.modules.ingestion.models import ParseStatus, RawLineItem
    from adc_backend.modules.tools.orchestration import process_run

    run = _setup_run_with_items(
        db_session,
        [
            {"item_number": "OK-1", "description": "Blue Widget", "price": "10.00"},
            {"item_number": "POISON", "description": "Poison Gadget", "price": "10.00"},
            {"item_number": "OK-3", "description": "Green Gizmo", "price": "10.00"},
        ],
    )
    summary = process_run(db_session, run.id, _DBPoisoningSPAPIClient(db_session, "Poison"), _StubKeepaClient())

    assert summary["error_rows"] == 1
    assert summary["processed_rows"] == 2
    items = {i.supplier_item_number: i for i in db_session.query(RawLineItem).filter_by(list_run_id=run.id).all()}
    assert items["POISON"].parse_status == ParseStatus.ERROR
    assert items["OK-3"].parse_status == ParseStatus.OK
    assert items["OK-3"].product_match_id is not None


class _CountingKeepaClient(_StubKeepaClient):
    def __init__(self):
        self.calls: list[str] = []

    def get_product(self, asin: str, stats_days: int = 90):
        self.calls.append(asin)
        return super().get_product(asin, stats_days)


def test_keepa_only_called_when_the_engine_reaches_the_velocity_check(db_session):
    # A NO_BUY on price can't be changed by velocity data - spending one
    # of the account's 1-per-minute Keepa tokens on it is pure waste.
    from adc_backend.modules.amazon.models import AmazonDataSnapshot
    from adc_backend.modules.rules.models import ClassificationLabel, ClassificationResult
    from adc_backend.modules.tools.orchestration import process_run

    run = _setup_run_with_items(
        db_session,
        [
            {"item_number": "CHEAP", "description": "Blue Widget", "price": "10.00"},  # ROI 50% -> BUY candidate
            {"item_number": "PRICEY", "description": "Red Gadget", "price": "30.00"},  # negative ROI -> NO_BUY
        ],
    )
    keepa = _CountingKeepaClient()
    process_run(db_session, run.id, _StubSPAPIClient(), keepa)

    assert len(keepa.calls) == 1
    results = {r.classification for r in db_session.query(ClassificationResult).filter_by(list_run_id=run.id)}
    assert results == {ClassificationLabel.BUY, ClassificationLabel.NO_BUY}
    snapshots = db_session.query(AmazonDataSnapshot).filter_by(list_run_id=run.id).all()
    with_velocity = [s for s in snapshots if s.sales_rank_drops_30d is not None]
    assert len(with_velocity) == 1 and with_velocity[0].asin == keepa.calls[0]


class _FailingKeepaClient:
    def get_product(self, asin: str, stats_days: int = 90):
        raise RuntimeError("Keepa down")


def test_keepa_failure_on_a_buy_candidate_routes_to_review_not_buy(db_session):
    from adc_backend.modules.approvals.models import ApprovalQueue
    from adc_backend.modules.review.models import ManualReviewQueue
    from adc_backend.modules.rules.models import ClassificationLabel, ClassificationResult
    from adc_backend.modules.tools.orchestration import process_run

    run = _setup_run_with_items(db_session, [{"item_number": "ABC-1", "description": "Blue Widget", "price": "10.00"}])
    process_run(db_session, run.id, _StubSPAPIClient(), _FailingKeepaClient())

    result = db_session.query(ClassificationResult).filter_by(list_run_id=run.id).one()
    assert result.classification == ClassificationLabel.REVIEW
    assert db_session.query(ApprovalQueue).filter_by(analysis_result_ref=result.id).count() == 0
    entry = db_session.query(ManualReviewQueue).filter_by(list_run_id=run.id).one()
    assert "Keepa" in entry.reason_notes


class _CountingSPAPIClient(_StubSPAPIClient):
    def __init__(self):
        super().__init__()
        self.searched: list[str] = []

    def search_catalog_items(self, keywords: str, marketplace_id: str = "", brand: str | None = None):
        self.searched.append(keywords)
        return super().search_catalog_items(keywords, marketplace_id, brand)


class _SimulatedCrash(Exception):
    pass


def test_interrupted_run_resumes_from_last_checkpoint():
    # Commits for real (that's the point), so it uses its own session and
    # NO_BUY-priced rows - nothing lands in the shared approval queue.
    from adc_backend.db import models  # noqa: F401
    from adc_backend.db.base import get_sessionmaker
    from adc_backend.db.core_models import ListRun, ListRunStatus
    from adc_backend.modules.ingestion.models import RawLineItem
    from adc_backend.modules.tools.orchestration import process_run

    session = get_sessionmaker()()
    try:
        specs = [{"item_number": f"R-{n}", "description": f"Resume Widget {n}", "price": "30.00"} for n in range(1, 6)]
        run = _setup_run_with_items(session, specs)
        session.commit()
        run_id = run.id

        def crash_after_first_checkpoint(_run):
            raise _SimulatedCrash()

        first_client = _CountingSPAPIClient()
        with pytest.raises(_SimulatedCrash):
            process_run(session, run_id, first_client, _StubKeepaClient(), checkpoint_every=2, on_checkpoint=crash_after_first_checkpoint)
        session.rollback()  # what worker.py does on an unexpected exception
        assert len(first_client.searched) == 2

        session.expire_all()
        interrupted = session.get(ListRun, run_id)
        assert interrupted.status == ListRunStatus.PROCESSING
        assert interrupted.processed_rows == 2  # visible progress, committed at the checkpoint
        assert interrupted.last_progress_at is not None

        second_client = _CountingSPAPIClient()
        summary = process_run(session, run_id, second_client, _StubKeepaClient(), checkpoint_every=2)
        session.commit()

        assert summary["resumed"] is True
        assert len(second_client.searched) == 3  # only the 3 rows not yet checkpointed
        assert summary["processed_rows"] == 5
        assert summary["total_rows"] == 5
        finished = session.get(ListRun, run_id)
        assert finished.status == ListRunStatus.COMPLETED
        assert finished.completed_at is not None
        assert session.query(RawLineItem).filter_by(list_run_id=run_id, processed_at=None).count() == 0
    finally:
        session.rollback()
        session.close()


def test_single_item_reprocess_after_owner_picks_a_upc_candidate(db_session):
    from adc_backend.modules.matching.engine import confirm_match
    from adc_backend.modules.matching.models import MatchSource, MatchStatus, ProductMatch
    from adc_backend.modules.rules.models import ClassificationLabel, ClassificationResult
    from adc_backend.modules.tools.orchestration import process_single_item

    from adc_backend.modules.ingestion.models import RawLineItem
    from adc_backend.modules.normalization.service import apply_normalization

    run = _setup_run_with_items(db_session, [{"item_number": "MULTI-1", "description": "Blue Widget", "price": "10.00"}])
    apply_normalization(db_session, run.id)
    item = db_session.query(RawLineItem).filter_by(list_run_id=run.id).one()
    match = ProductMatch(
        supplier_id=run.supplier_id,
        supplier_item_number="MULTI-1",
        asin=None,
        match_status=MatchStatus.PROPOSED,
        match_source=MatchSource.UPC,
        candidate_asins=["B000000001", "B000000002"],
    )
    db_session.add(match)
    db_session.flush()
    item.product_match_id = match.id
    db_session.flush()

    confirm_match(db_session, match.id, confirmed_by="owner", asin="B000000002")
    assert match.match_source == MatchSource.UPC  # picked from the barcode's own candidates
    assert float(match.match_confidence) == 1.0

    outcome = process_single_item(db_session, run.id, item.id, _StubSPAPIClient(), _StubKeepaClient())
    assert outcome["ok"] is True
    result = db_session.query(ClassificationResult).filter_by(raw_line_item_id=item.id).one()
    assert result.classification == ClassificationLabel.BUY
