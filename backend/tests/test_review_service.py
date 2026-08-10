"""DB-backed review service tests: routing persists correctly and resolved entries aren't silently reopened."""

from __future__ import annotations

import os
import uuid

import pytest

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


def _make_bundle_item(db_session):
    from adc_backend.db.core_models import ListRun, SourceFileType, Supplier
    from adc_backend.modules.ingestion.models import LineItemType, RawLineItem

    supplier = Supplier(name=f"Test Supplier {uuid.uuid4()}", code=f"test-{uuid.uuid4().hex[:8]}")
    db_session.add(supplier)
    db_session.flush()
    run = ListRun(
        supplier_id=supplier.id,
        source_file_s3_key="s3://x/y.csv",
        source_file_original_filename="y.csv",
        source_file_type=SourceFileType.CSV,
    )
    db_session.add(run)
    db_session.flush()
    item = RawLineItem(list_run_id=run.id, row_number=1, raw_data={}, item_type=LineItemType.DISPLAY_BUNDLE)
    db_session.add(item)
    db_session.flush()
    return run, item


def test_evaluate_and_route_creates_entry(db_session):
    from adc_backend.modules.review.models import ManualReviewQueue, ReviewReason
    from adc_backend.modules.review.service import evaluate_and_route

    run, item = _make_bundle_item(db_session)
    entry = evaluate_and_route(db_session, run.id, item.id)
    assert entry is not None
    assert entry.reason == ReviewReason.DISPLAY_BUNDLE_SKU

    count = db_session.query(ManualReviewQueue).filter_by(raw_line_item_id=item.id).count()
    assert count == 1


def test_resolved_entry_not_reopened_on_rerun(db_session):
    from adc_backend.modules.review.models import ReviewStatus
    from adc_backend.modules.review.service import evaluate_and_route, resolve_review_entry

    run, item = _make_bundle_item(db_session)
    entry = evaluate_and_route(db_session, run.id, item.id)
    resolve_review_entry(db_session, entry.id, resolved_by="owner@example.com", notes="Confirmed as a bundle, skipped.")

    # Re-run (e.g. normalization re-applied) - still routes the same way,
    # but must not flip a resolved entry back to pending.
    entry_again = evaluate_and_route(db_session, run.id, item.id)
    assert entry_again.id == entry.id
    assert entry_again.status == ReviewStatus.RESOLVED
