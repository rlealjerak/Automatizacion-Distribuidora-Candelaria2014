"""
Tests for reminder_job.py's timing/state logic against real local
Postgres. Uses a fake TelegramNotifier (same stub-client pattern as
test_orchestration.py's stub SP-API/Keepa clients) - this validates the
job's control flow and state transitions, not real Telegram delivery.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


class FakeNotifier:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[str] = []

    def send_reminder(self, text: str) -> None:
        from adc_backend.telegram_notifier import TelegramNotifierError

        if self.fail:
            raise TelegramNotifierError("simulated failure")
        self.sent.append(text)


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


@pytest.fixture
def active_item(db_session):
    from adc_backend.db.core_models import ListRun, SourceFileType, Supplier
    from adc_backend.modules.amazon.models import AmazonDataSnapshot
    from adc_backend.modules.approvals.service import create_from_classification
    from adc_backend.modules.ingestion.models import RawLineItem
    from adc_backend.modules.rules.models import ClassificationLabel, ClassificationResult

    supplier = Supplier(name=f"Reminder Test Supplier {uuid.uuid4()}", code=f"reminder-{uuid.uuid4().hex[:8]}")
    db_session.add(supplier)
    db_session.flush()
    run = ListRun(supplier_id=supplier.id, source_file_s3_key="s3://x/y.csv", source_file_original_filename="y.csv", source_file_type=SourceFileType.CSV)
    db_session.add(run)
    db_session.flush()
    item = RawLineItem(list_run_id=run.id, row_number=1, raw_data={}, unit_price=Decimal("10.00"), description="Reminder Widget")
    db_session.add(item)
    db_session.flush()
    snapshot = AmazonDataSnapshot(list_run_id=run.id, raw_line_item_id=item.id, asin="B0REMIND01", current_price=Decimal("20.00"))
    db_session.add(snapshot)
    db_session.flush()
    trace = [{"rule": "financial_calculation", "result": "info", "reasoning": "x", "inputs": {"unit_cost": "10.00", "sell_price": "20.00", "profit": "5.00"}}]
    result = ClassificationResult(list_run_id=run.id, raw_line_item_id=item.id, amazon_data_snapshot_id=snapshot.id, classification=ClassificationLabel.BUY, roi=Decimal(50), margin=Decimal(25), rule_trace=trace)
    db_session.add(result)
    db_session.flush()
    return create_from_classification(db_session, result.id)


def test_no_active_item_is_a_noop(db_session):
    from adc_backend.reminder_job import check_and_send_reminder

    notifier = FakeNotifier()
    outcome = check_and_send_reminder(db_session, notifier, datetime.now(UTC))
    assert outcome == "no_active_item"
    assert notifier.sent == []


def test_not_yet_due_is_a_noop(db_session, active_item):
    from adc_backend.reminder_job import check_and_send_reminder

    notifier = FakeNotifier()
    outcome = check_and_send_reminder(db_session, notifier, datetime.now(UTC) + timedelta(minutes=2))
    assert outcome == "not_due"
    assert notifier.sent == []


def test_five_minute_threshold_sends_first_reminder(db_session, active_item):
    from adc_backend.modules.approvals.models import ApprovalStatus
    from adc_backend.reminder_job import check_and_send_reminder

    notifier = FakeNotifier()
    now = active_item.pending_started_at + timedelta(minutes=5, seconds=1)
    outcome = check_and_send_reminder(db_session, notifier, now)

    assert outcome == "reminder_sent:1"
    assert len(notifier.sent) == 1
    db_session.refresh(active_item)
    assert active_item.status == ApprovalStatus.REMINDER_1
    assert active_item.reminder_count == 1
    assert active_item.last_reminder_at == now


def test_reminder_cadence_through_sixth_then_holding(db_session, active_item):
    from adc_backend.modules.approvals.models import ApprovalStatus
    from adc_backend.reminder_job import check_and_send_reminder

    notifier = FakeNotifier()
    now = active_item.pending_started_at + timedelta(minutes=5, seconds=1)
    check_and_send_reminder(db_session, notifier, now)  # reminder_1

    for expected_count in range(2, 7):
        now = now + timedelta(minutes=10, seconds=1)
        outcome = check_and_send_reminder(db_session, notifier, now)
        assert outcome == f"reminder_sent:{expected_count}"

    db_session.refresh(active_item)
    assert active_item.status == ApprovalStatus.REMINDER_6
    assert active_item.reminder_count == 6
    assert len(notifier.sent) == 6

    # A 7th cycle sends no further message - transitions to holding instead.
    now = now + timedelta(minutes=10, seconds=1)
    outcome = check_and_send_reminder(db_session, notifier, now)
    assert outcome == "holding"
    assert len(notifier.sent) == 6  # unchanged - no 7th message ever sent
    db_session.refresh(active_item)
    assert active_item.status == ApprovalStatus.HOLDING

    # Holding is permanent from this job's perspective - never re-checked into a message.
    now = now + timedelta(days=30)
    outcome = check_and_send_reminder(db_session, notifier, now)
    assert outcome == "not_due"
    assert len(notifier.sent) == 6


def test_failed_send_does_not_advance_state(db_session, active_item):
    from adc_backend.reminder_job import check_and_send_reminder

    notifier = FakeNotifier(fail=True)
    now = active_item.pending_started_at + timedelta(minutes=5, seconds=1)
    outcome = check_and_send_reminder(db_session, notifier, now)

    assert outcome == "send_failed"
    db_session.refresh(active_item)
    assert active_item.reminder_count == 0
    assert active_item.last_reminder_at is None
