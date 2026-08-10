"""
Round-trip verification of the ORM models against a real Postgres instance
(local Docker, port 5433 - see docker-compose.yml + .env.example).

Skips instead of failing if DATABASE_URL isn't set, so `pytest` still
passes in an environment without a database (e.g. a CI step that only
lints), but running it for real is how step 2 is actually verified - not
just that `alembic upgrade head` succeeds, but that inserts, foreign
keys, the persistent-mapping unique constraint, and relationship
navigation all behave as designed.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - see backend/docker-compose.yml to run a local Postgres",
)


@pytest.fixture
def db_session():
    from adc_backend.db import models  # noqa: F401 - registers all tables
    from adc_backend.db.base import get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_supplier_list_run_line_item_round_trip(db_session):
    from adc_backend.db.core_models import ListRun, ListRunStatus, SourceFileType, Supplier
    from adc_backend.modules.ingestion.models import LineItemType, ParseStatus, RawLineItem

    supplier = Supplier(name=f"Test Supplier {uuid.uuid4()}", code=f"test-{uuid.uuid4().hex[:8]}")
    db_session.add(supplier)
    db_session.flush()

    run = ListRun(
        supplier_id=supplier.id,
        source_file_s3_key="s3://adc-prod-supplier-files/test.xlsx",
        source_file_original_filename="test.xlsx",
        source_file_type=SourceFileType.EXCEL,
        status=ListRunStatus.UPLOADED,
    )
    db_session.add(run)
    db_session.flush()

    item = RawLineItem(
        list_run_id=run.id,
        row_number=1,
        raw_data={"UNIT PRICE": "$2.90", "ITEM #": "ABC-123"},
        supplier_item_number="ABC-123",
        unit_price=Decimal("2.90"),
        item_type=LineItemType.STANDARD,
        parse_status=ParseStatus.OK,
    )
    db_session.add(item)
    db_session.flush()

    db_session.refresh(run)
    assert run.supplier.id == supplier.id
    assert len(run.raw_line_items) == 1
    assert run.raw_line_items[0].supplier_item_number == "ABC-123"


def test_product_match_persistent_mapping_unique_constraint(db_session):
    from adc_backend.db.core_models import Supplier
    from adc_backend.modules.matching.models import MatchSource, MatchStatus, ProductMatch

    supplier = Supplier(name=f"Test Supplier {uuid.uuid4()}", code=f"test-{uuid.uuid4().hex[:8]}")
    db_session.add(supplier)
    db_session.flush()

    match = ProductMatch(
        supplier_id=supplier.id,
        supplier_item_number="ABC-123",
        asin="B000000001",
        match_confidence=Decimal("0.9500"),
        match_status=MatchStatus.CONFIRMED,
        match_source=MatchSource.AUTO,
    )
    db_session.add(match)
    db_session.flush()

    # Same supplier + item number must be rejected - this is the persistent
    # mapping CLAUDE.md requires to be looked up/updated, never duplicated.
    dupe = ProductMatch(
        supplier_id=supplier.id,
        supplier_item_number="ABC-123",
        asin="B000000002",
        match_status=MatchStatus.PROPOSED,
        match_source=MatchSource.AUTO,
    )
    db_session.add(dupe)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_rule_trace_required_on_classification_result(db_session):
    """
    rule_trace is NOT NULL by design - CLAUDE.md requires the owner always
    sees the reasoning behind a classification, never just the label.
    """
    from adc_backend.db.core_models import ListRun, ListRunStatus, SourceFileType, Supplier
    from adc_backend.modules.ingestion.models import RawLineItem
    from adc_backend.modules.rules.models import ClassificationLabel, ClassificationResult

    supplier = Supplier(name=f"Test Supplier {uuid.uuid4()}", code=f"test-{uuid.uuid4().hex[:8]}")
    db_session.add(supplier)
    db_session.flush()
    run = ListRun(
        supplier_id=supplier.id,
        source_file_s3_key="s3://x/y.xlsx",
        source_file_original_filename="y.xlsx",
        source_file_type=SourceFileType.EXCEL,
        status=ListRunStatus.PROCESSING,
    )
    db_session.add(run)
    db_session.flush()
    item = RawLineItem(list_run_id=run.id, row_number=1, raw_data={})
    db_session.add(item)
    db_session.flush()

    # Note: passing rule_trace=None explicitly would store the JSON literal
    # `null` (valid JSONB data, not a SQL NULL) and would NOT trip this
    # constraint - the real test is omitting the field entirely.
    bad_result = ClassificationResult(
        list_run_id=run.id,
        raw_line_item_id=item.id,
        classification=ClassificationLabel.BUY,
    )
    db_session.add(bad_result)
    with pytest.raises(IntegrityError):
        db_session.flush()
