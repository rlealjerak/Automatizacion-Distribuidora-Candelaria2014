"""DB-backed tests for CSV export and run comparison (build-order step 11)."""

from __future__ import annotations

import csv
import io
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


def _make_run(db_session, supplier=None):
    from adc_backend.db.core_models import ListRun, SourceFileType, Supplier

    if supplier is None:
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
    return supplier, run


def _make_classified_item(db_session, run, item_number, unit_price, classification, roi):
    from sqlalchemy import func, select

    from adc_backend.modules.ingestion.models import RawLineItem
    from adc_backend.modules.rules.models import ClassificationResult

    next_row_number = (db_session.execute(select(func.count()).where(RawLineItem.list_run_id == run.id)).scalar() or 0) + 1
    item = RawLineItem(
        list_run_id=run.id,
        row_number=next_row_number,
        raw_data={},
        supplier_item_number=item_number,
        unit_price=Decimal(str(unit_price)),
    )
    db_session.add(item)
    db_session.flush()
    result = ClassificationResult(
        list_run_id=run.id,
        raw_line_item_id=item.id,
        classification=classification,
        roi=Decimal(str(roi)),
        rule_trace=[{"rule": "test", "result": "info", "reasoning": "test fixture"}],
    )
    db_session.add(result)
    db_session.flush()
    return item


def test_export_csv_contains_expected_rows(db_session):
    from adc_backend.modules.rules.models import ClassificationLabel
    from adc_backend.modules.tools.export import export_run_results_csv

    _, run = _make_run(db_session)
    _make_classified_item(db_session, run, "ABC-1", "10.00", ClassificationLabel.BUY, "50")
    _make_classified_item(db_session, run, "ABC-2", "20.00", ClassificationLabel.NO_BUY, "2")

    csv_bytes = export_run_results_csv(db_session, run.id)
    rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
    assert len(rows) == 2
    assert {r["supplier_item_number"] for r in rows} == {"ABC-1", "ABC-2"}
    assert rows[0]["classification"] in ("buy", "no_buy")


def test_export_csv_filters_by_classification(db_session):
    from adc_backend.modules.rules.models import ClassificationLabel
    from adc_backend.modules.tools.export import export_run_results_csv

    _, run = _make_run(db_session)
    _make_classified_item(db_session, run, "ABC-1", "10.00", ClassificationLabel.BUY, "50")
    _make_classified_item(db_session, run, "ABC-2", "20.00", ClassificationLabel.NO_BUY, "2")

    csv_bytes = export_run_results_csv(db_session, run.id, classification_filter=["buy"])
    rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
    assert len(rows) == 1
    assert rows[0]["supplier_item_number"] == "ABC-1"


def test_compare_runs_flags_changed_classification(db_session):
    from adc_backend.modules.rules.models import ClassificationLabel
    from adc_backend.modules.tools.export import compare_runs

    supplier, run_a = _make_run(db_session)
    _, run_b = _make_run(db_session, supplier=supplier)

    # Same supplier item, different outcome across two runs (e.g. price changed).
    _make_classified_item(db_session, run_a, "ABC-1", "10.00", ClassificationLabel.NO_BUY, "2")
    _make_classified_item(db_session, run_b, "ABC-1", "10.00", ClassificationLabel.BUY, "50")
    # Unchanged item, present in both.
    _make_classified_item(db_session, run_a, "ABC-2", "5.00", ClassificationLabel.BUY, "40")
    _make_classified_item(db_session, run_b, "ABC-2", "5.00", ClassificationLabel.BUY, "40")
    # Only in run_b (new item this time).
    _make_classified_item(db_session, run_b, "ABC-3", "8.00", ClassificationLabel.REVIEW, "15")

    comparison = compare_runs(db_session, run_a.id, run_b.id)
    by_item = {row["supplier_item_number"]: row for row in comparison}

    assert by_item["ABC-1"]["changed"] is True
    assert by_item["ABC-1"]["run_a_classification"] == "no_buy"
    assert by_item["ABC-1"]["run_b_classification"] == "buy"

    assert by_item["ABC-2"]["changed"] is False

    assert by_item["ABC-3"]["run_a_classification"] is None
    assert by_item["ABC-3"]["run_b_classification"] == "review"
    assert by_item["ABC-3"]["changed"] is True  # None -> "review" counts as a change
