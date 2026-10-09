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


# --- audit log (2026-10-08 decision: audit-log every approve_decision call, including rejected ones) ---


def test_resolve_active_item_logs_approved_event(db_session, supplier):
    from sqlalchemy import select

    from adc_backend.modules.approvals.models import ApprovalAuditLog
    from adc_backend.modules.approvals.service import (
        create_from_classification,
        resolve_active_item,
    )
    from adc_backend.modules.rules.models import ClassificationLabel

    active = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)
    resolve_active_item(db_session, str(active.id), "approved", note="go ahead", actor="openclaw-mcp")

    log = db_session.execute(select(ApprovalAuditLog).where(ApprovalAuditLog.approval_id == active.id)).scalar_one()
    assert log.ok is True
    assert log.event.value == "approved"
    assert log.action == "resolve"
    assert log.actor == "openclaw-mcp"
    assert log.note == "go ahead"


def test_resolve_active_item_logs_rejected_call_for_wrong_id(db_session, supplier):
    from sqlalchemy import select

    from adc_backend.modules.approvals.models import ApprovalAuditLog
    from adc_backend.modules.approvals.service import (
        create_from_classification,
        resolve_active_item,
    )
    from adc_backend.modules.rules.models import ClassificationLabel

    create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)  # active
    queued = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)

    resolve_active_item(db_session, str(queued.id), "approved")

    log = db_session.execute(
        select(ApprovalAuditLog).where(ApprovalAuditLog.requested_approval_id == str(queued.id))
    ).scalar_one()
    assert log.ok is False
    assert log.event.value == "rejected"
    assert log.action == "resolve"


def test_resolve_active_item_logs_rejected_call_for_invalid_id(db_session):
    from sqlalchemy import select

    from adc_backend.modules.approvals.models import ApprovalAuditLog
    from adc_backend.modules.approvals.service import resolve_active_item

    resolve_active_item(db_session, "not-a-uuid", "approved")

    log = db_session.execute(
        select(ApprovalAuditLog).where(ApprovalAuditLog.requested_approval_id == "not-a-uuid")
    ).scalar_one()
    assert log.ok is False
    assert log.approval_id is None
    assert log.event.value == "rejected"


# --- revoke_decision (2026-10-08 decision: approvals must be reversible) ---


def test_revoke_decision_becomes_active_again_when_queue_empty(db_session, supplier):
    from adc_backend.modules.approvals.models import ApprovalStatus
    from adc_backend.modules.approvals.service import (
        create_from_classification,
        resolve_active_item,
        revoke_decision,
    )
    from adc_backend.modules.rules.models import ClassificationLabel

    only = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)
    resolve_active_item(db_session, str(only.id), "approved")  # queue now fully empty

    out = revoke_decision(db_session, str(only.id), reason="wrong tap", revoked_by="rob")
    assert out.ok is True
    assert out.new_status == "pending"

    db_session.refresh(only)
    assert only.status == ApprovalStatus.PENDING
    assert only.is_active_item is True
    assert only.pending_started_at is not None
    assert only.reminder_count == 0
    assert only.last_reminder_at is None
    assert only.resolution is None
    assert only.resolved_at is None


def test_revoke_decision_inserts_at_front_of_queue_when_another_item_active(db_session, supplier):
    from adc_backend.modules.approvals.models import ApprovalStatus
    from adc_backend.modules.approvals.service import (
        create_from_classification,
        resolve_active_item,
        revoke_decision,
    )
    from adc_backend.modules.rules.models import ClassificationLabel

    first = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)
    second = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)
    resolve_active_item(db_session, str(first.id), "approved")  # second now active
    third = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)  # queued, position 1

    out = revoke_decision(db_session, str(first.id), reason="test misfire", revoked_by="rob")
    assert out.ok is True
    assert out.new_status == "queued"

    db_session.refresh(first)
    db_session.refresh(second)
    db_session.refresh(third)

    assert first.status == ApprovalStatus.QUEUED
    assert first.is_active_item is False
    assert first.queue_position == 1
    assert first.resolution is None
    assert second.is_active_item is True  # untouched - never force-evicted
    assert third.queue_position == 2  # bumped back behind the revoked item


def test_revoke_decision_rejects_pending_item(db_session, supplier):
    from adc_backend.modules.approvals.service import create_from_classification, revoke_decision
    from adc_backend.modules.rules.models import ClassificationLabel

    active = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)

    out = revoke_decision(db_session, str(active.id), reason="oops", revoked_by="rob")
    assert out.ok is False
    assert out.error_kind == "not_approved"


def test_revoke_decision_rejects_second_call(db_session, supplier):
    from adc_backend.modules.approvals.service import (
        create_from_classification,
        resolve_active_item,
        revoke_decision,
    )
    from adc_backend.modules.rules.models import ClassificationLabel

    only = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)
    resolve_active_item(db_session, str(only.id), "approved")

    first = revoke_decision(db_session, str(only.id), reason="first", revoked_by="rob")
    second = revoke_decision(db_session, str(only.id), reason="second", revoked_by="rob")

    assert first.ok is True
    assert second.ok is False
    assert second.error_kind == "not_approved"


def test_revoke_decision_rejects_invalid_id(db_session):
    from adc_backend.modules.approvals.service import revoke_decision

    out = revoke_decision(db_session, "not-a-uuid", reason="x", revoked_by="rob")
    assert out.ok is False
    assert out.error_kind == "invalid_id"


def test_revoke_decision_rejects_unknown_id(db_session):
    from adc_backend.modules.approvals.service import revoke_decision

    out = revoke_decision(db_session, str(uuid.uuid4()), reason="x", revoked_by="rob")
    assert out.ok is False
    assert out.error_kind == "not_found"


def test_revoke_decision_audit_log_preserves_approved_event_in_order(db_session, supplier):
    from sqlalchemy import select

    from adc_backend.modules.approvals.models import ApprovalAuditLog
    from adc_backend.modules.approvals.service import (
        create_from_classification,
        resolve_active_item,
        revoke_decision,
    )
    from adc_backend.modules.rules.models import ClassificationLabel

    item = create_from_classification(db_session, _make_result(db_session, supplier.id, ClassificationLabel.BUY).id)
    resolve_active_item(db_session, str(item.id), "approved", note="first pass looked good")
    revoke_decision(db_session, str(item.id), reason="misfire", revoked_by="rob")

    events = db_session.execute(
        select(ApprovalAuditLog).where(ApprovalAuditLog.approval_id == item.id).order_by(ApprovalAuditLog.created_at)
    ).scalars().all()

    assert [e.event.value for e in events] == ["approved", "approval_revoked"]
    assert events[0].ok is True and events[0].note == "first pass looked good"
    assert events[1].ok is True and events[1].note == "misfire" and events[1].actor == "rob"
