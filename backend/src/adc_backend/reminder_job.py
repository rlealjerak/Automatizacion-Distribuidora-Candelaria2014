"""
Reminder-scheduling job for the OpenClaw approval queue.

One-shot script, not a loop: EventBridge Scheduler invokes this fresh
every minute via ecs:RunTask (see infra/modules/ecs_cluster) - the
existing SQS worker (worker.py) can't do this instead, because it's
purely event-driven (a long-poll loop reacting only to queue messages,
no timer of any kind - see docs/openclaw/OPENCLAW_TECHNICAL_SPEC.md
Section 5 for the full "why not the worker" reasoning).

Implements the timing half of the LOCKED state machine
(docs/openclaw/OPENCLAW_APPROVAL_STATE_MACHINE_SPEC.md Section 3): 5 min
initial wait, then a reminder every 10 min, up to 6, then indefinite
`holding` with no further messages - ever, no timeout, no expiry. Most
invocations of this script are a no-op (nothing due yet, given the
1-minute cadence against 5/10-minute thresholds), which is expected, not
a bug.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from adc_backend.db.base import get_sessionmaker
from adc_backend.modules.approvals.models import ApprovalStatus
from adc_backend.modules.approvals.service import list_queue
from adc_backend.telegram_notifier import TelegramNotifier, TelegramNotifierError

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # same reasoning as main.py/worker.py - never log request URLs/keys
logger = logging.getLogger(__name__)

INITIAL_WAIT = timedelta(minutes=5)
REMINDER_INTERVAL = timedelta(minutes=10)
MAX_REMINDERS = 6

_REMINDER_STATUS_BY_COUNT = {
    1: ApprovalStatus.REMINDER_1,
    2: ApprovalStatus.REMINDER_2,
    3: ApprovalStatus.REMINDER_3,
    4: ApprovalStatus.REMINDER_4,
    5: ApprovalStatus.REMINDER_5,
    6: ApprovalStatus.REMINDER_6,
}


def check_and_send_reminder(db: Session, notifier: TelegramNotifier, now: datetime) -> str:
    """
    Returns a short outcome string for logging/testing: "no_active_item",
    "not_due", "reminder_sent:N", "holding", or "send_failed".
    """
    item, _queued = list_queue(db)
    if item is None:
        return "no_active_item"

    if item.status == ApprovalStatus.HOLDING:
        return "not_due"  # holding is terminal-until-client-replies - never re-checked into a message

    if item.reminder_count == 0:
        due_at = item.pending_started_at + INITIAL_WAIT
    else:
        due_at = item.last_reminder_at + REMINDER_INTERVAL

    if now < due_at:
        return "not_due"

    if item.reminder_count >= MAX_REMINDERS:
        item.status = ApprovalStatus.HOLDING
        db.flush()
        logger.info("Approval item %s exhausted %s reminders with no reply - moved to holding", item.id, MAX_REMINDERS)
        return "holding"

    next_count = item.reminder_count + 1
    try:
        notifier.send_reminder(
            f"Reminder ({next_count}/{MAX_REMINDERS}) - awaiting your approval:\n\n{item.summary}"
        )
    except TelegramNotifierError as e:
        # Do NOT advance reminder_count/last_reminder_at on a failed send -
        # the client would silently miss a reminder while the clock kept
        # ticking toward holding. Next minute's invocation retries.
        logger.error("Reminder send failed for approval item %s, will retry next run: %s", item.id, e)
        return "send_failed"

    item.reminder_count = next_count
    item.last_reminder_at = now
    item.status = _REMINDER_STATUS_BY_COUNT[next_count]
    db.flush()
    logger.info("Sent reminder %s/%s for approval item %s", next_count, MAX_REMINDERS, item.id)
    return f"reminder_sent:{next_count}"


def main() -> None:
    session = get_sessionmaker()()
    try:
        notifier = TelegramNotifier.from_settings()
        outcome = check_and_send_reminder(session, notifier, datetime.now(UTC))
        session.commit()
        logger.info("reminder_job run complete: %s", outcome)
    except Exception:
        session.rollback()
        logger.exception("reminder_job run failed")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
