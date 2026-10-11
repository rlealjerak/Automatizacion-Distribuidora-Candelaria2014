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


# --- initial "Nueva decisión pendiente" notice (2026-10-11 decision) ---


def test_initial_notice_sent_immediately_regardless_of_elapsed_time(db_session, active_item):
    from adc_backend.reminder_job import check_and_send_reminder

    notifier = FakeNotifier()
    # No time has elapsed at all - the initial notice still fires, unlike
    # a reminder, which would need to wait for INITIAL_WAIT.
    outcome = check_and_send_reminder(db_session, notifier, active_item.pending_started_at)

    assert outcome == "initial_notice_sent"
    assert len(notifier.sent) == 1
    assert "Nueva decisión pendiente" in notifier.sent[0]
    assert "Recordatorio" not in notifier.sent[0]
    db_session.refresh(active_item)
    assert active_item.initial_notice_sent_at == active_item.pending_started_at
    # Sending the initial notice must not itself advance reminder state.
    assert active_item.reminder_count == 0


def test_initial_notice_sent_at_most_once(db_session, active_item):
    from adc_backend.reminder_job import check_and_send_reminder

    notifier = FakeNotifier()
    now = active_item.pending_started_at
    first = check_and_send_reminder(db_session, notifier, now)
    second = check_and_send_reminder(db_session, notifier, now + timedelta(seconds=1))

    assert first == "initial_notice_sent"
    assert second == "not_due"  # reminder timing, not a resend of the initial notice
    assert len(notifier.sent) == 1


def test_initial_notice_failed_send_does_not_mark_sent(db_session, active_item):
    from adc_backend.reminder_job import check_and_send_reminder

    failing = FakeNotifier(fail=True)
    outcome = check_and_send_reminder(db_session, failing, active_item.pending_started_at)
    assert outcome == "send_failed"
    db_session.refresh(active_item)
    assert active_item.initial_notice_sent_at is None

    # Next run (a working notifier) still sends it - nothing was skipped.
    working = FakeNotifier()
    outcome = check_and_send_reminder(db_session, working, active_item.pending_started_at + timedelta(seconds=5))
    assert outcome == "initial_notice_sent"
    assert len(working.sent) == 1


def _send_initial_notice(db_session, active_item):
    """Every reminder-timing test below needs this out of the way first."""
    from adc_backend.reminder_job import check_and_send_reminder

    check_and_send_reminder(db_session, FakeNotifier(), active_item.pending_started_at)
    db_session.refresh(active_item)


# --- reminder cadence (unchanged from the LOCKED state machine spec; the
# initial notice above is a new, additional step, not a replacement) ---


def test_not_yet_due_is_a_noop(db_session, active_item):
    from adc_backend.reminder_job import check_and_send_reminder

    _send_initial_notice(db_session, active_item)
    notifier = FakeNotifier()
    outcome = check_and_send_reminder(db_session, notifier, active_item.pending_started_at + timedelta(minutes=2))
    assert outcome == "not_due"
    assert notifier.sent == []


def test_five_minute_threshold_sends_first_reminder(db_session, active_item):
    from adc_backend.modules.approvals.models import ApprovalStatus
    from adc_backend.reminder_job import check_and_send_reminder

    _send_initial_notice(db_session, active_item)
    notifier = FakeNotifier()
    now = active_item.pending_started_at + timedelta(minutes=5, seconds=1)
    outcome = check_and_send_reminder(db_session, notifier, now)

    assert outcome == "reminder_sent:1"
    assert len(notifier.sent) == 1
    assert notifier.sent[0].startswith("Recordatorio 1/6 - Decisión pendiente")
    db_session.refresh(active_item)
    assert active_item.status == ApprovalStatus.REMINDER_1
    assert active_item.reminder_count == 1
    assert active_item.last_reminder_at == now


def test_reminder_cadence_through_sixth_then_holding(db_session, active_item):
    from adc_backend.modules.approvals.models import ApprovalStatus
    from adc_backend.reminder_job import check_and_send_reminder

    _send_initial_notice(db_session, active_item)
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

    _send_initial_notice(db_session, active_item)
    notifier = FakeNotifier(fail=True)
    now = active_item.pending_started_at + timedelta(minutes=5, seconds=1)
    outcome = check_and_send_reminder(db_session, notifier, now)

    assert outcome == "send_failed"
    db_session.refresh(active_item)
    assert active_item.reminder_count == 0
    assert active_item.last_reminder_at is None


# --- _format_fetched_at_es (2026-10-11 decision: unambiguous, America/New_York, with age) ---


def test_format_fetched_at_es_matches_the_worked_example():
    from adc_backend.reminder_job import _format_fetched_at_es

    # "10 oct, 9:43 p. m. (hace 12 h)" - the exact example given. UTC
    # 2026-10-11 01:43 -> America/New_York (EDT, UTC-4) 2026-10-10 21:43.
    fetched = "2026-10-11T01:43:00"
    now = datetime(2026, 10, 11, 13, 43, tzinfo=UTC)  # 12h later
    text, is_stale = _format_fetched_at_es(fetched, now)
    assert text == "10 oct, 9:43 p. m. (hace 12 h)"
    assert is_stale is False


def test_format_fetched_at_es_stale_past_24h():
    from adc_backend.reminder_job import _format_fetched_at_es

    fetched = "2026-10-10T01:43:00"
    now = datetime(2026, 10, 11, 13, 43, tzinfo=UTC)  # 36h later
    text, is_stale = _format_fetched_at_es(fetched, now)
    assert is_stale is True
    assert "hace 1 d" in text


def test_format_fetched_at_es_none_when_no_data():
    from adc_backend.reminder_job import _format_fetched_at_es

    assert _format_fetched_at_es(None, datetime.now(UTC)) == (None, False)
