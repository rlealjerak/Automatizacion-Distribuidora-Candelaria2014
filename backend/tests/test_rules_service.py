"""DB-backed test that rules/service.py correctly wires ORM data into the pure engine and persists the result."""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

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


def test_classify_line_item_persists_result_and_is_rerunnable(db_session):
    from adc_backend.db.core_models import ListRun, SourceFileType, Supplier
    from adc_backend.modules.amazon.models import AmazonDataSnapshot
    from adc_backend.modules.ingestion.models import RawLineItem
    from adc_backend.modules.rules.config import seed_default_rules_config
    from adc_backend.modules.rules.models import ClassificationLabel, ClassificationResult
    from adc_backend.modules.rules.service import classify_line_item

    seed_default_rules_config(db_session)

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
    item = RawLineItem(list_run_id=run.id, row_number=1, raw_data={}, unit_price=Decimal("10.00"))
    db_session.add(item)
    db_session.flush()
    snapshot = AmazonDataSnapshot(
        list_run_id=run.id,
        raw_line_item_id=item.id,
        asin="B000000001",
        current_price=Decimal("20.00"),
        buy_box_price=Decimal("20.00"),
        referral_fee=Decimal("3.00"),
        fba_fee=Decimal("2.00"),
        seller_count=2,
    )
    db_session.add(snapshot)
    db_session.flush()

    result = classify_line_item(db_session, run.id, item.id)
    assert result.classification == ClassificationLabel.BUY
    assert result.roi == Decimal(50)
    assert len(result.rule_trace) > 0

    # Re-running (e.g. after a re-fetch of live data) updates the same row,
    # doesn't create a duplicate - ClassificationResult.raw_line_item_id is
    # unique per the step-2 schema design.
    result_again = classify_line_item(db_session, run.id, item.id)
    assert result_again.id == result.id

    count = db_session.query(ClassificationResult).filter_by(raw_line_item_id=item.id).count()
    assert count == 1
