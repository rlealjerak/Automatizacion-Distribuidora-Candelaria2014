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
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from adc_backend.db.base import get_sessionmaker
from adc_backend.modules.approvals.models import ApprovalQueue, ApprovalStatus
from adc_backend.modules.approvals.service import list_queue
from adc_backend.modules.rules.reasoning_es import format_money_es, format_pct_es
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

# The business is Spanish-speaking (Distribuidora Candelaria 2014) -
# only the two classifications that ever reach the approval queue
# (modules/approvals/service.py::APPROVAL_GATED_LABELS) need a mapping.
_RECOMMENDATION_ES = {
    "buy": "COMPRAR",
    "high_risk": "COMPRA DE ALTO RIESGO (revisar con cuidado antes de decidir)",
}


def _format_fetched_at(raw: str | None) -> str | None:
    """
    `summary_payload["fetched_at"]` is a naive-datetime isoformat string
    (AmazonDataSnapshot.fetched_at has no timezone=True - stored via
    Postgres's own now(), which is UTC on this RDS instance same as
    everywhere else in this project). Rendered DD/MM/YYYY HH:MM, the
    Spanish date convention, not the US one.
    """
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.strftime("%d/%m/%Y %H:%M") + " UTC"


def _build_message_text(item: ApprovalQueue, reminder_count: int | None) -> str:
    """
    Spanish-language message, built directly from `summary_payload`'s
    structured fields - deliberately NOT `item.summary` (English,
    meant for internal/audit use - see service.py::_build_summary) - so
    the two never get mixed.

    `reminder_count=None` renders the one-time "Nueva decisión
    pendiente" notice (see check_and_send_reminder). Any int 1-6 renders
    a numbered reminder instead, with "Recordatorio N/6" at the very
    top of the message (moved there from a footer, 2026-10-11 - the
    client should see which reminder this is before reading anything
    else).

    Short code (e.g. "D0001"), not the UUID, both in the header and in
    the reply commands - and formatted as `/aprobar_<code>` /
    `/rechazar_<code>` specifically (underscore, not a space) because
    Telegram renders a `/word_with_underscores` command as a tappable
    link; `/aprobar <code>` as free text would not be. Resolving the
    code back to a real approval_id is already case-insensitive
    end-to-end (modules/approvals/service.py::_resolve_approval_id
    upper-cases before comparing) - "toca" below is accurate, not
    aspirational: the client can tap with either case and it works.
    """
    payload = item.summary_payload or {}
    product = payload.get("description") or "(sin descripción)"
    if payload.get("brand"):
        product = f"{product} ({payload['brand']})"
    supplier = payload.get("supplier_name") or "(proveedor desconocido)"
    recommendation = _RECOMMENDATION_ES.get(payload.get("classification"), (payload.get("classification") or "").upper())

    if reminder_count is None:
        header = f"Nueva decisión pendiente (ID: {item.short_code})"
    else:
        header = f"Recordatorio {reminder_count}/{MAX_REMINDERS} - Decisión pendiente (ID: {item.short_code})"

    lines = [
        header,
        "",
        f"Producto: {product}",
        f"Proveedor: {supplier}",
        f"Recomendación: {recommendation}",
    ]
    if payload.get("roi_pct") is not None and payload.get("margin_pct") is not None:
        lines.append(f"ROI: {format_pct_es(payload['roi_pct'])} | Margen: {format_pct_es(payload['margin_pct'])}")

    # Case size, investment, and profit per case AND per unit - one
    # line, per explicit instruction (2026-10-11), not three.
    if payload.get("case_quantity") and payload.get("case_unit") and payload.get("capital_required") is not None and payload.get("profit") is not None:
        try:
            profit_per_case = Decimal(payload["profit"]) * Decimal(payload["case_quantity"])
            profit_per_case_str = format_money_es(profit_per_case)
        except InvalidOperation:
            profit_per_case_str = "?"
        lines.append(
            f"Presentación: {payload['case_quantity']} {payload['case_unit']} | "
            f"Inversión: {format_money_es(payload['capital_required'])} | "
            f"Ganancia: {format_money_es(payload['profit'])}/unidad, {profit_per_case_str}/caja"
        )

    fetched_at = _format_fetched_at(payload.get("fetched_at"))
    if fetched_at:
        lines.append(f"Datos de Amazon/Keepa obtenidos: {fetched_at}")

    if payload.get("por_que"):
        lines.append(f"Por qué: {payload['por_que']}")
    if payload.get("riesgo"):
        lines.append(f"Riesgo: {payload['riesgo']}")

    lines += [
        "",
        f"Para decidir, toca: /aprobar_{item.short_code} o /rechazar_{item.short_code}",
    ]
    return "\n".join(lines)


def check_and_send_reminder(db: Session, notifier: TelegramNotifier, now: datetime) -> str:
    """
    Returns a short outcome string for logging/testing: "no_active_item",
    "initial_notice_sent", "not_due", "reminder_sent:N", "holding", or
    "send_failed".

    The one-time "Nueva decisión pendiente" notice (initial_notice_sent_at
    IS NULL) is checked and sent *before* any reminder-timing logic -
    an item that just became active should hear about it on this very
    next poll, regardless of how long it's been pending. Same
    send-then-record discipline as reminders below: never marked sent
    on a failed Telegram call, so a transient failure retries next
    minute rather than silently skipping the client's only notice of a
    new decision. Decided 2026-10-11, closing the gap where the client's
    first-ever notice of a new item was a message literally labeled
    "Reminder (1/6)" of something that had never actually been announced.
    """
    item, _queued = list_queue(db)
    if item is None:
        return "no_active_item"

    if item.initial_notice_sent_at is None:
        try:
            notifier.send_reminder(_build_message_text(item, reminder_count=None))
        except TelegramNotifierError as e:
            logger.error("Initial notice send failed for approval item %s, will retry next run: %s", item.id, e)
            return "send_failed"
        item.initial_notice_sent_at = now
        db.flush()
        logger.info("Sent initial notice for approval item %s (%s)", item.id, item.short_code)
        return "initial_notice_sent"

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
        notifier.send_reminder(_build_message_text(item, reminder_count=next_count))
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
