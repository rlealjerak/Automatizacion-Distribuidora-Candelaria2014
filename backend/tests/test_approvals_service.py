"""DB-backed tests for modules/approvals/service.py against real local Postgres."""

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


def _make_result(db, supplier_id, classification, roi=None, margin=None, description="Widget 9000", brand="Acme", asin="B000000001"):
    from adc_backend.db.core_models import ListRun, SourceFileType
    from adc_backend.modules.amazon.models import AmazonDataSnapshot
    from adc_backend.modules.ingestion.models import RawLineItem
    from adc_backend.modules.matching.models import MatchSource, MatchStatus, ProductMatch
    from adc_backend.modules.rules.models import ClassificationResult

    run = ListRun(
        supplier_id=supplier_id,
        source_file_s3_key="s3://x/y.csv",
        source_file_original_filename="y.csv",
        source_file_type=SourceFileType.CSV,
    )
    db.add(run)
    db.flush()

    item = RawLineItem(
        list_run_id=run.id, row_number=1, raw_data={}, unit_price=Decimal("10.00"),
        description=description, brand=brand,
    )
    db.add(item)
    db.flush()

    match = ProductMatch(
        supplier_id=supplier_id, supplier_item_number=f"sku-{uuid.uuid4().hex[:6]}",
        asin=asin, match_status=MatchStatus.CONFIRMED, match_source=MatchSource.AUTO,
    )
    db.add(match)
    db.flush()
    item.product_match_id = match.id
    db.flush()

    snapshot = AmazonDataSnapshot(list_run_id=run.id, raw_line_item_id=item.id, asin=asin, current_price=Decimal("20.00"))
    db.add(snapshot)
    db.flush()

    trace = [
        {
            "rule": "financial_calculation", "result": "info", "reasoning": "x",
            "inputs": {"unit_cost": "10.00", "sell_price": "20.00", "profit": "5.00"},
        }
    ]
    if classification.value == "high_risk":
        trace.append({"rule": "amazon_has_buy_box", "result": "fail", "reasoning": "Amazon holds the buy box."})

    result = ClassificationResult(
        list_run_id=run.id, raw_line_item_id=item.id, amazon_data_snapshot_id=snapshot.id,
        classification=classification, roi=roi, margin=margin, rule_trace=trace,
    )
    db.add(result)
    db.flush()
    return result


@pytest.fixture
def supplier(db_session):
    from adc_backend.db.core_models import Supplier

    s = Supplier(name=f"Test Supplier {uuid.uuid4()}", code=f"test-{uuid.uuid4().hex[:8]}")
    db_session.add(s)
    db_session.flush()
    return s


def test_buy_creates_pending_when_queue_empty(db_session, supplier):
    from adc_backend.modules.approvals.models import ApprovalStatus
    from adc_backend.modules.approvals.service import create_from_classification
    from adc_backend.modules.rules.models import ClassificationLabel

    result = _make_result(db_session, supplier.id, ClassificationLabel.BUY, roi=Decimal("34.0"), margin=Decimal("18.0"))
    item = create_from_classification(db_session, result.id)

    assert item is not None
    assert item.status == ApprovalStatus.PENDING
    assert item.is_active_item is True
    assert item.queue_position is None
    assert item.pending_started_at is not None
    assert "BUY" in item.summary and "Widget 9000" in item.summary and "Acme" in item.summary
    assert item.summary_payload["asin"] == "B000000001"
    assert item.summary_payload["profit"] == "5.00"


@pytest.mark.parametrize("label_name", ["REVIEW", "NEGOTIATE", "NO_BUY"])
def test_non_gated_labels_create_nothing(db_session, supplier, label_name):
    from adc_backend.modules.approvals.service import create_from_classification
    from adc_backend.modules.rules.models import ClassificationLabel

    result = _make_result(db_session, supplier.id, ClassificationLabel[label_name])
    assert create_from_classification(db_session, result.id) is None


def test_second_buy_queues_behind_active(db_session, supplier):
    from adc_backend.modules.approvals.models import ApprovalStatus
    from adc_backend.modules.approvals.service import create_from_classification
    from adc_backend.modules.rules.models import ClassificationLabel

    first = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)
    second = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)

    assert first.is_active_item is True
    assert second.status == ApprovalStatus.QUEUED
    assert second.queue_position == 1


def test_high_risk_queues_after_existing_queued_buy_items(db_session, supplier):
    from adc_backend.modules.approvals.service import create_from_classification
    from adc_backend.modules.rules.models import ClassificationLabel

    create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)  # active
    buy_2 = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)  # queued 1
    high_risk = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.HIGH_RISK).id)  # queued 2
    buy_3 = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)  # inserted ahead of high_risk

    assert buy_2.queue_position == 1
    assert buy_3.queue_position == 2  # inserted after the last BUY item, ahead of high_risk
    assert high_risk.queue_position == 3  # bumped back
    assert "HIGH RISK" in high_risk.summary


def test_resolve_active_item_advances_queue(db_session, supplier):
    from adc_backend.modules.approvals.models import ApprovalStatus
    from adc_backend.modules.approvals.service import (
        create_from_classification,
        resolve_active_item,
    )
    from adc_backend.modules.rules.models import ClassificationLabel

    active = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)
    queued = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)

    out = resolve_active_item(db_session, str(active.id), "approved", note="looks good")
    assert out.ok is True
    assert out.resolution == "approved"
    assert out.next_active_item.id == queued.id

    db_session.refresh(active)
    db_session.refresh(queued)
    assert active.status == ApprovalStatus.APPROVED
    assert active.is_active_item is False
    assert active.resolution_note == "looks good"
    assert queued.status == ApprovalStatus.PENDING
    assert queued.is_active_item is True
    assert queued.queue_position is None


def test_resolve_active_item_rejects_wrong_id(db_session, supplier):
    from adc_backend.modules.approvals.service import (
        create_from_classification,
        resolve_active_item,
    )
    from adc_backend.modules.rules.models import ClassificationLabel

    active = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)
    queued = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)

    out = resolve_active_item(db_session, str(queued.id), "approved")
    assert out.ok is False
    assert out.active_approval_id == str(active.id)


def test_resolve_active_item_idempotent_replay(db_session, supplier):
    from adc_backend.modules.approvals.service import (
        create_from_classification,
        resolve_active_item,
    )
    from adc_backend.modules.rules.models import ClassificationLabel

    active = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)
    first = resolve_active_item(db_session, str(active.id), "approved")
    replay = resolve_active_item(db_session, str(active.id), "approved")

    assert first.ok is True and replay.ok is True
    assert replay.resolved_at == first.resolved_at


def test_resolve_active_item_conflicting_replay_rejected(db_session, supplier):
    from adc_backend.modules.approvals.service import (
        create_from_classification,
        resolve_active_item,
    )
    from adc_backend.modules.rules.models import ClassificationLabel

    active = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)
    resolve_active_item(db_session, str(active.id), "approved")
    conflict = resolve_active_item(db_session, str(active.id), "denied")

    assert conflict.ok is False
    assert "already resolved as 'approved'" in conflict.error


def test_reorder_queue_moves_item_to_front(db_session, supplier):
    from adc_backend.modules.approvals.service import create_from_classification, reorder_queue
    from adc_backend.modules.rules.models import ClassificationLabel

    create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)  # active
    first_queued = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)
    second_queued = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)

    reordered = reorder_queue(db_session, str(second_queued.id))
    db_session.refresh(first_queued)

    assert reordered.queue_position == 1
    assert first_queued.queue_position == 2


def test_reorder_queue_rejects_non_queued_item(db_session, supplier):
    from adc_backend.modules.approvals.service import (
        ApprovalsServiceError,
        create_from_classification,
        reorder_queue,
    )
    from adc_backend.modules.rules.models import ClassificationLabel

    active = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)
    with pytest.raises(ApprovalsServiceError):
        reorder_queue(db_session, str(active.id))
