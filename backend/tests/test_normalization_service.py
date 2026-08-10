"""
DB-backed normalization tests: propose -> confirm -> apply against real
local Postgres. Same skip-if-not-configured pattern as test_db_models.py.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set - see backend/docker-compose.yml",
)


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


def _make_run_with_rows(db_session, rows: list[dict]):
    from adc_backend.db.core_models import ListRun, SourceFileType, Supplier
    from adc_backend.modules.ingestion.models import RawLineItem

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

    for i, raw_data in enumerate(rows, start=1):
        db_session.add(RawLineItem(list_run_id=run.id, row_number=i, raw_data=raw_data))
    db_session.flush()
    return run


def test_propose_confirm_apply_end_to_end(db_session):
    from adc_backend.db.core_models import ListRunStatus
    from adc_backend.modules.ingestion.models import LineItemType, ParseStatus, RawLineItem
    from adc_backend.modules.normalization.service import (
        ConfirmedMapping,
        apply_normalization,
        confirm_mapping,
        propose_mapping_for_run,
    )
    from adc_backend.modules.normalization.unit_lookup import seed_default_unit_lookup

    seed_default_unit_lookup(db_session)

    run = _make_run_with_rows(
        db_session,
        [
            {"ITEM #": "ABC-1", "DESCRIPTION": "Widget", "SALE PRICE": "$2.90", "UNIT": "EA"},
            {"ITEM #": "DISP-1", "DESCRIPTION": "9 COLOR DISPLAY", "SALE PRICE": "45.00", "UNIT": "DP"},
            {"ITEM #": "", "DESCRIPTION": "Mystery item", "SALE PRICE": "1.00", "UNIT": "EA"},
        ],
    )

    run = propose_mapping_for_run(db_session, run.id)
    assert run.proposed_column_mapping["supplier_item_number"]["header"] == "ITEM #"
    assert run.proposed_column_mapping["unit_price"]["header"] == "SALE PRICE"

    run = confirm_mapping(
        db_session,
        run.id,
        ConfirmedMapping(
            supplier_item_number="ITEM #",
            description="DESCRIPTION",
            brand=None,
            unit_price="SALE PRICE",
            case_quantity=None,
            case_unit="UNIT",
        ),
        confirmed_by="test-owner",
    )
    assert run.status == ListRunStatus.MAPPING_CONFIRMED
    assert run.column_mapping_confirmed_by == "test-owner"

    counts = apply_normalization(db_session, run.id)
    assert counts == {"ok": 1, "needs_review": 1, "error": 1}

    items = {i.raw_data["ITEM #"]: i for i in db_session.query(RawLineItem).filter_by(list_run_id=run.id).all()}

    standard = items["ABC-1"]
    assert standard.item_type == LineItemType.STANDARD
    assert standard.parse_status == ParseStatus.OK
    assert standard.unit_price == Decimal("2.90")
    assert standard.case_unit_normalized == "EACH"

    bundle = items["DISP-1"]
    assert bundle.item_type == LineItemType.DISPLAY_BUNDLE
    assert bundle.parse_status == ParseStatus.NEEDS_REVIEW
    assert bundle.unit_price is None
    # DP is an explicitly-unconfirmed unit (see unit_lookup.py) - normalized
    # name is still surfaced, but the seed data intentionally has no
    # multiplier for it.
    assert bundle.case_unit_normalized == "DISPLAY_PACK"

    error_item = items[""]
    assert error_item.parse_status == ParseStatus.ERROR


def test_confirm_mapping_requires_supplier_item_number(db_session):
    from adc_backend.modules.normalization.service import (
        ConfirmedMapping,
        NormalizationError,
        confirm_mapping,
    )

    run = _make_run_with_rows(db_session, [{"A": "1"}])
    with pytest.raises(NormalizationError):
        confirm_mapping(
            db_session,
            run.id,
            ConfirmedMapping(
                supplier_item_number=None,
                description="A",
                brand=None,
                unit_price=None,
                case_quantity=None,
                case_unit=None,
            ),
            confirmed_by="test-owner",
        )


def test_seed_default_unit_lookup_is_idempotent(db_session):
    from adc_backend.modules.normalization.unit_lookup import seed_default_unit_lookup

    first = seed_default_unit_lookup(db_session)
    second = seed_default_unit_lookup(db_session)
    assert first > 0
    assert second == 0
