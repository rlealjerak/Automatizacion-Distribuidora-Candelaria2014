"""
Approval-queue service: creates queue items from classification results,
lists the queue, resolves the active item, and reorders queued items.

Every public function here is called from exactly one of two places per
design: the MCP tools (modules/mcp_server/) that OpenClaw calls, and the
one hook added to modules/tools/orchestration.py where a classification
result is finalized (see docs/openclaw/BACKEND_CHANGES_FOR_OPENCLAW.md
Section 4 - "both paths funnel through the same create-an-approval-item
function"). Nothing in this file talks to Telegram or MCP directly - see
reminder_job.py and modules/mcp_server/ for those integration points.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from adc_backend.db.core_models import ListRun, Supplier
from adc_backend.modules.amazon.models import AmazonDataSnapshot
from adc_backend.modules.approvals.models import (
    ApprovalAuditEvent,
    ApprovalAuditLog,
    ApprovalItemType,
    ApprovalQueue,
    ApprovalResolution,
    ApprovalStatus,
)
from adc_backend.modules.ingestion.models import RawLineItem
from adc_backend.modules.matching.models import ProductMatch
from adc_backend.modules.rules.models import ClassificationLabel, ClassificationResult

# Labels that create an approval item at all. Per explicit owner decision
# (2026-08-28) this is BUY *and* HIGH_RISK, not BUY-only as the original
# spec docs floated - HIGH_RISK is "not excluded, just flagged as higher
# risk/difficulty" (rules/engine.py), so it's still a real purchase
# decision requiring the owner's explicit approval. REVIEW/NEGOTIATE/
# NO_BUY never reach this at all - REVIEW already has its own manual
# review queue (modules/review), and NEGOTIATE/NO_BUY aren't "ready to
# purchase" decisions.
APPROVAL_GATED_LABELS = (ClassificationLabel.BUY, ClassificationLabel.HIGH_RISK)


class ApprovalsServiceError(Exception):
    pass


@dataclass
class ResolveResult:
    ok: bool
    approval_id: str | None = None
    resolution: str | None = None
    resolved_at: datetime | None = None
    next_active_item: ApprovalQueue | None = None
    error: str | None = None
    active_approval_id: str | None = None


@dataclass
class RevokeResult:
    ok: bool
    approval_id: str | None = None
    new_status: str | None = None
    error: str | None = None
    # "invalid_id" | "not_found" | "not_approved" - lets callers (the
    # REST router) map to a sensible HTTP status without string-sniffing
    # `error`.
    error_kind: str | None = None


def _log_audit_event(
    db: Session,
    *,
    approval_id: uuid.UUID | None,
    requested_approval_id: str,
    action: str,
    event: ApprovalAuditEvent,
    ok: bool,
    actor: str | None,
    note: str | None = None,
    error: str | None = None,
) -> None:
    """
    Every call into resolve_active_item/revoke_decision logs exactly one
    of these, success or not - append-only, see ApprovalAuditLog's
    docstring. Never raises on its own account; a logging bug must never
    be what breaks an approve/deny/revoke call.
    """
    db.add(
        ApprovalAuditLog(
            approval_id=approval_id,
            requested_approval_id=requested_approval_id,
            action=action,
            event=event,
            ok=ok,
            actor=actor,
            note=note,
            error=error,
        )
    )
    db.flush()


def _get_active_item(db: Session) -> ApprovalQueue | None:
    return db.execute(select(ApprovalQueue).where(ApprovalQueue.is_active_item.is_(True))).scalar_one_or_none()


def _get_queued_items_ordered(db: Session) -> list[ApprovalQueue]:
    return list(
        db.execute(
            select(ApprovalQueue)
            .where(ApprovalQueue.status == ApprovalStatus.QUEUED)
            .order_by(ApprovalQueue.queue_position)
        ).scalars()
    )


def _parse_approval_id(approval_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(approval_id)
    except (ValueError, AttributeError, TypeError):
        return None


def _active_id_str(db: Session) -> str | None:
    active = _get_active_item(db)
    return str(active.id) if active else None


def _financial_calc_inputs(rule_trace: list[dict]) -> dict[str, Decimal]:
    """
    Pulls unit_cost/sell_price/profit straight out of the
    'financial_calculation' rule_trace entry that rules/engine.py::classify
    always appends before it can reach BUY or HIGH_RISK - reusing the
    exact numbers already computed there rather than recomputing (and
    risking drift) from the raw snapshot/line-item rows.
    """
    for entry in rule_trace:
        if entry.get("rule") == "financial_calculation":
            raw = entry.get("inputs", {})
            try:
                return {
                    "unit_cost": Decimal(raw["unit_cost"]),
                    "sell_price": Decimal(raw["sell_price"]),
                    "profit": Decimal(raw["profit"]),
                }
            except (KeyError, InvalidOperation):
                break
    # Shouldn't normally happen - classify() only reaches BUY/HIGH_RISK
    # after this entry is appended - but never guess a financial number.
    return {"unit_cost": None, "sell_price": None, "profit": None}


def _build_summary(
    classification: ClassificationLabel,
    description: str | None,
    brand: str | None,
    asin: str | None,
    supplier_name: str,
    roi_pct: Decimal | None,
    margin_pct: Decimal | None,
    financials: dict,
    rule_trace: list[dict],
) -> str:
    label_word = "BUY" if classification == ClassificationLabel.BUY else "HIGH-RISK BUY"
    product = description or "(no description on file)"
    if brand:
        product = f"{product} ({brand})"

    parts = [
        f"{label_word}: {product} — ASIN {asin or 'unmatched'} from {supplier_name}.",
        f"ROI {roi_pct:.1f}%, margin {margin_pct:.1f}%" if roi_pct is not None and margin_pct is not None else "ROI/margin unavailable",
    ]
    if financials.get("unit_cost") is not None:
        parts[-1] += f", cost ${financials['unit_cost']}, sell ${financials['sell_price']}, est. profit ${financials['profit']}"
    parts[-1] += "."

    if classification == ClassificationLabel.HIGH_RISK:
        risk_reasons = [
            e["reasoning"] for e in rule_trace if e.get("rule") in ("amazon_has_buy_box", "seller_count_high_risk") and e.get("result") == "fail"
        ]
        if risk_reasons:
            parts.append("Flagged HIGH RISK: " + " ".join(risk_reasons))

    return " ".join(parts)


def create_from_classification(db: Session, classification_result_id: uuid.UUID) -> ApprovalQueue | None:
    """
    Creates an approval_queue row for a BUY/HIGH_RISK classification, or
    returns None for any other label (no-op, not an error - most
    classifications never reach the approval queue at all).

    New items are priority-inserted, per explicit owner decision: a new
    BUY item lands after the last queued BUY item (ahead of any queued
    HIGH_RISK items); a new HIGH_RISK item is appended at the end. This
    only affects where a *new* item lands, never reorders an
    already-queued item - reordering an existing item is exclusively the
    reorder_queue() function below, invoked only on explicit client
    instruction (OPENCLAW_APPROVAL_STATE_MACHINE_SPEC.md Section 5c).
    """
    result = db.get(ClassificationResult, classification_result_id)
    if result is None:
        raise ApprovalsServiceError(f"No classification_result with id {classification_result_id}")
    if result.classification not in APPROVAL_GATED_LABELS:
        return None

    line_item = db.get(RawLineItem, result.raw_line_item_id)
    list_run = db.get(ListRun, result.list_run_id)
    supplier = db.get(Supplier, list_run.supplier_id)
    match = db.get(ProductMatch, line_item.product_match_id) if line_item and line_item.product_match_id else None
    snapshot = db.get(AmazonDataSnapshot, result.amazon_data_snapshot_id) if result.amazon_data_snapshot_id else None

    financials = _financial_calc_inputs(result.rule_trace)
    asin = match.asin if match else (snapshot.asin if snapshot else None)

    summary = _build_summary(
        classification=result.classification,
        description=line_item.description if line_item else None,
        brand=line_item.brand if line_item else None,
        asin=asin,
        supplier_name=supplier.name if supplier else "(unknown supplier)",
        roi_pct=result.roi,
        margin_pct=result.margin,
        financials=financials,
        rule_trace=result.rule_trace,
    )
    summary_payload = {
        "asin": asin,
        "description": line_item.description if line_item else None,
        "brand": line_item.brand if line_item else None,
        "supplier_name": supplier.name if supplier else None,
        "classification": result.classification.value,
        "roi_pct": str(result.roi) if result.roi is not None else None,
        "margin_pct": str(result.margin) if result.margin is not None else None,
        "unit_cost": str(financials["unit_cost"]) if financials["unit_cost"] is not None else None,
        "sell_price": str(financials["sell_price"]) if financials["sell_price"] is not None else None,
        "profit": str(financials["profit"]) if financials["profit"] is not None else None,
    }

    item = ApprovalQueue(
        analysis_result_ref=result.id,
        type=ApprovalItemType.PURCHASE_RECOMMENDATION,
        summary=summary,
        summary_payload=summary_payload,
    )

    active = _get_active_item(db)
    if active is None:
        item.status = ApprovalStatus.PENDING
        item.is_active_item = True
        item.pending_started_at = datetime.now(UTC)
        item.queue_position = None
    else:
        queued = _get_queued_items_ordered(db)
        is_high_risk = result.classification == ClassificationLabel.HIGH_RISK
        if is_high_risk:
            insert_index = len(queued)  # append at the very end
        else:
            # Insert right after the last queued BUY item (queue is
            # always kept BUY-block-then-HIGH_RISK-block by this same
            # rule, so the first non-BUY item marks the boundary).
            insert_index = 0
            for existing in queued:
                if existing.summary_payload.get("classification") == ClassificationLabel.BUY.value:
                    insert_index += 1
                else:
                    break
        for existing in queued[insert_index:]:
            existing.queue_position += 1
        item.status = ApprovalStatus.QUEUED
        item.is_active_item = False
        item.queue_position = insert_index + 1

    db.add(item)
    db.flush()
    return item


def list_queue(db: Session) -> tuple[ApprovalQueue | None, list[ApprovalQueue]]:
    return _get_active_item(db), _get_queued_items_ordered(db)


def _advance_queue(db: Session) -> ApprovalQueue | None:
    queued = _get_queued_items_ordered(db)
    if not queued:
        return None
    next_item = queued[0]
    next_item.status = ApprovalStatus.PENDING
    next_item.is_active_item = True
    next_item.pending_started_at = datetime.now(UTC)
    next_item.queue_position = None
    for remaining in queued[1:]:
        remaining.queue_position -= 1
    return next_item


def resolve_active_item(
    db: Session, approval_id: str, resolution: str, note: str | None = None, actor: str = "openclaw-mcp"
) -> ResolveResult:
    """
    Resolves the active item only - the server-side backstop for the
    one-active-item model (OPENCLAW_TECHNICAL_SPEC.md Section 4.2): a
    call targeting anything else is rejected, never silently applied to
    "whatever's active instead."

    Idempotent on replay: a duplicate call with the same approval_id +
    resolution against an item already resolved that way returns the
    same success shape rather than erroring - OpenClaw's MCP retry
    behavior on timeout is unconfirmed (spec Section 6, item 1), and this
    is the safe answer regardless. A replay with a *different* resolution
    than what's already stored is a genuine conflict, not something to
    silently overwrite - "no financially consequential action ever
    executes automatically" extends to never letting an ambiguous retry
    flip a decision that's already been made.

    Every return path logs exactly one ApprovalAuditLog row, success or
    not - "audit-log every call, including rejected ones" per the
    2026-10-08 owner decision alongside the revoke path below.
    """
    approval_uuid = _parse_approval_id(approval_id)
    if approval_uuid is None:
        error = f"approval_id {approval_id!r} is not a valid id"
        _log_audit_event(
            db, approval_id=None, requested_approval_id=approval_id, action="resolve",
            event=ApprovalAuditEvent.REJECTED, ok=False, actor=actor, note=note, error=error,
        )
        return ResolveResult(ok=False, error=error, active_approval_id=_active_id_str(db))

    item = db.get(ApprovalQueue, approval_uuid)
    active = _get_active_item(db)

    if item is not None and active is not None and item.id == active.id:
        try:
            resolution_enum = ApprovalResolution(resolution)
        except ValueError:
            error = f"resolution {resolution!r} must be 'approved' or 'denied'"
            _log_audit_event(
                db, approval_id=item.id, requested_approval_id=approval_id, action="resolve",
                event=ApprovalAuditEvent.REJECTED, ok=False, actor=actor, note=note, error=error,
            )
            return ResolveResult(ok=False, error=error, active_approval_id=str(active.id))
        item.resolution = resolution_enum
        item.resolved_at = datetime.now(UTC)
        item.resolution_note = note
        item.status = ApprovalStatus.APPROVED if resolution_enum == ApprovalResolution.APPROVED else ApprovalStatus.DENIED
        item.is_active_item = False
        # Flushed separately, before _advance_queue() sets a new row's
        # is_active_item=True: both are UPDATEs in the same transaction,
        # and uq_approval_queue_one_active_item is checked per-statement
        # (not deferred) - without this, SQLAlchemy can order the two
        # UPDATEs so the new active row's flag is set while the old one
        # still reads true, tripping the unique index. Found by actually
        # running the resolve-then-advance test, not assumed.
        db.flush()
        next_item = _advance_queue(db)
        db.flush()
        _log_audit_event(
            db, approval_id=item.id, requested_approval_id=approval_id, action="resolve",
            event=ApprovalAuditEvent.APPROVED if resolution_enum == ApprovalResolution.APPROVED else ApprovalAuditEvent.DENIED,
            ok=True, actor=actor, note=note,
        )
        return ResolveResult(
            ok=True,
            approval_id=str(item.id),
            resolution=resolution_enum.value,
            resolved_at=item.resolved_at,
            next_active_item=next_item,
        )

    if item is not None and item.resolution is not None:
        if item.resolution.value == resolution:
            # Idempotent replay - still logged (it's a real call that
            # happened), just doesn't re-apply anything.
            _log_audit_event(
                db, approval_id=item.id, requested_approval_id=approval_id, action="resolve",
                event=ApprovalAuditEvent.APPROVED if item.resolution == ApprovalResolution.APPROVED else ApprovalAuditEvent.DENIED,
                ok=True, actor=actor, note=note,
            )
            return ResolveResult(
                ok=True,
                approval_id=str(item.id),
                resolution=item.resolution.value,
                resolved_at=item.resolved_at,
                next_active_item=_get_active_item(db),
            )
        error = f"approval_id {approval_id} was already resolved as {item.resolution.value!r}, cannot resolve as {resolution!r}"
        _log_audit_event(
            db, approval_id=item.id, requested_approval_id=approval_id, action="resolve",
            event=ApprovalAuditEvent.REJECTED, ok=False, actor=actor, note=note, error=error,
        )
        return ResolveResult(ok=False, error=error, active_approval_id=_active_id_str(db))

    error = f"approval_id {approval_id} is not the active item"
    _log_audit_event(
        db, approval_id=item.id if item is not None else None, requested_approval_id=approval_id, action="resolve",
        event=ApprovalAuditEvent.REJECTED, ok=False, actor=actor, note=note, error=error,
    )
    return ResolveResult(ok=False, error=error, active_approval_id=_active_id_str(db))


def revoke_decision(db: Session, approval_id: str, reason: str, revoked_by: str) -> RevokeResult:
    """
    Reverses an APPROVED item back into the queue - approved -> * only,
    never any other starting state. Decided 2026-10-08: approvals must
    be reversible (Rob's own test misfires against prod, or a genuine
    mistaken approval) - but the approval being reversed is never
    erased, only ever superseded by a new `approval_revoked`
    ApprovalAuditLog row sitting permanently next to the original
    `approved` one (see that model's docstring).

    Race-safety: the approved->* transition is a single conditional
    UPDATE ... WHERE status = 'approved', not a Python
    read-then-compare-then-write - two concurrent revoke calls against
    the same row can only ever have one succeed; the second's WHERE
    clause simply matches zero rows once the first has committed, no
    explicit locking needed.

    One-active-item complication the original request's literal wording
    ("approved -> pending") didn't fully account for: by the time an
    item reaches APPROVED, _advance_queue() has already promoted
    whatever was next in line to be the new active/pending item, so
    writing this item straight back to PENDING would collide with
    uq_approval_queue_one_active_item (two rows both claiming to be
    "the" active item). Resolution, flagged here rather than silently
    assumed: if nothing is currently active (the queue had emptied),
    this item becomes the active/pending item again, with a fresh timer
    - otherwise it goes to the FRONT of the queue (queue_position=1)
    instead of force-evicting whatever the client is currently looking
    at. Either way it "reappears in list_queue" per the spec, and no
    reminder fires as a side effect of the revoke itself - reminders
    only resume if/when this item is later promoted back to active
    through the ordinary _advance_queue flow, same as any other queued
    item ("reminders do not restart automatically").

    --- FUTURE GUARD (not yet applicable - nothing to check against) ---
    Once Priority 2 introduces purchase-order drafts generated from
    approvals, add a check HERE, before the conditional UPDATE below:
    refuse the revoke outright if a PO draft already exists/was sent for
    this approval_id. No such table exists anywhere in this schema yet,
    so nothing is stubbed - this comment is the marker for whoever
    builds that phase.
    """
    approval_uuid = _parse_approval_id(approval_id)
    if approval_uuid is None:
        error = f"approval_id {approval_id!r} is not a valid id"
        _log_audit_event(
            db, approval_id=None, requested_approval_id=approval_id, action="revoke",
            event=ApprovalAuditEvent.REJECTED, ok=False, actor=revoked_by, note=reason, error=error,
        )
        return RevokeResult(ok=False, error=error, error_kind="invalid_id")

    result = db.execute(
        update(ApprovalQueue)
        .where(ApprovalQueue.id == approval_uuid, ApprovalQueue.status == ApprovalStatus.APPROVED)
        .values(resolution=None, resolved_at=None, resolution_note=None)
    )
    if result.rowcount == 0:
        existing = db.get(ApprovalQueue, approval_uuid)
        if existing is None:
            error = f"approval_id {approval_id} does not exist"
            error_kind = "not_found"
        else:
            error = f"approval_id {approval_id} is not approved (current status: {existing.status.value!r}) - only approved items can be revoked"
            error_kind = "not_approved"
        _log_audit_event(
            db, approval_id=existing.id if existing else None, requested_approval_id=approval_id, action="revoke",
            event=ApprovalAuditEvent.REJECTED, ok=False, actor=revoked_by, note=reason, error=error,
        )
        return RevokeResult(ok=False, error=error, error_kind=error_kind)

    db.flush()
    item = db.get(ApprovalQueue, approval_uuid)

    active = _get_active_item(db)
    if active is None:
        item.status = ApprovalStatus.PENDING
        item.is_active_item = True
        item.pending_started_at = datetime.now(UTC)
        item.last_reminder_at = None
        item.reminder_count = 0
        item.queue_position = None
    else:
        queued = _get_queued_items_ordered(db)
        for existing_q in queued:
            existing_q.queue_position += 1
        item.status = ApprovalStatus.QUEUED
        item.is_active_item = False
        item.queue_position = 1

    db.flush()
    _log_audit_event(
        db, approval_id=item.id, requested_approval_id=approval_id, action="revoke",
        event=ApprovalAuditEvent.REVOKED, ok=True, actor=revoked_by, note=reason,
    )
    return RevokeResult(ok=True, approval_id=str(item.id), new_status=item.status.value)


def reorder_queue(db: Session, approval_id: str) -> ApprovalQueue:
    """
    Moves a queued item to the front. Only ever invoked on explicit
    client instruction - that constraint is enforced by OpenClaw's own
    agent instructions (it's a prompt-level rule, not something this
    schema alone can enforce - see OPENCLAW_TECHNICAL_SPEC.md Section 4.3),
    not by anything checked here.
    """
    approval_uuid = _parse_approval_id(approval_id)
    if approval_uuid is None:
        raise ApprovalsServiceError(f"approval_id {approval_id!r} is not a valid id")

    item = db.get(ApprovalQueue, approval_uuid)
    if item is None or item.status != ApprovalStatus.QUEUED:
        raise ApprovalsServiceError(f"approval_id {approval_id} is not a queued item")

    queued = _get_queued_items_ordered(db)
    queued.remove(item)
    for remaining in queued:
        remaining.queue_position += 1
    item.queue_position = 1
    db.flush()
    return item
