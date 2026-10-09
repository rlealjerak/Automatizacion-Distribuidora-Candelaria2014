"""
Approvals module: workflow/state-tracking for the OpenClaw Telegram
approval flow. Deliberately its own module, not folded into `rules` -
`approval_queue` is "where is this item in the Telegram back-and-forth"
(queue position, reminder timers), conceptually distinct from
`classification_results`, which is the business-rules *conclusion*. The
only link between the two schemas is `ApprovalQueue.analysis_result_ref`
- see docs/openclaw/OPENCLAW_TECHNICAL_SPEC.md Section 5 for the full
reasoning and docs/openclaw/OPENCLAW_APPROVAL_STATE_MACHINE_SPEC.md for
the state machine this table implements (LOCKED, owner-approved - do not
change state semantics here without checking that doc first).

Note on which classifications land here at all: per an explicit owner
decision (not in the original spec docs, which only floated BUY), both
BUY and HIGH_RISK create an approval item - HIGH_RISK is "not excluded,
just flagged as higher risk/difficulty" per rules/engine.py, so it's
still a real purchase decision that needs the owner's explicit approval,
just a riskier one. See modules/approvals/service.py for the priority
rule this implies (BUY items queue ahead of HIGH_RISK items).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from adc_backend.db.base import Base


class ApprovalItemType(str, enum.Enum):
    PURCHASE_RECOMMENDATION = "purchase_recommendation"
    # Extensible for Phase 2+ (e.g. replenishment decisions) without a
    # schema change - see OPENCLAW_TECHNICAL_SPEC.md Section 4.1.


class ApprovalStatus(str, enum.Enum):
    QUEUED = "queued"
    PENDING = "pending"
    REMINDER_1 = "reminder_1"
    REMINDER_2 = "reminder_2"
    REMINDER_3 = "reminder_3"
    REMINDER_4 = "reminder_4"
    REMINDER_5 = "reminder_5"
    REMINDER_6 = "reminder_6"
    HOLDING = "holding"
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalResolution(str, enum.Enum):
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalAuditEvent(str, enum.Enum):
    APPROVED = "approved"
    DENIED = "denied"
    REVOKED = "approval_revoked"
    # A call that didn't apply at all - bad/unknown id, wrong state,
    # conflicting replay. Logged same as any other event: the point of
    # this table is what was *attempted*, not just what succeeded.
    REJECTED = "rejected"


class ApprovalQueue(Base):
    """
    One row per approval-requiring recommendation. Only one row at a time
    may have `is_active_item=True` (the item currently `pending`/
    `reminder_N`/`holding`) - enforced by the partial unique index below,
    same pattern as BusinessRulesConfig.is_active (rules/models.py) for
    "at most one active row of this kind", checked at the DB level, not
    just trusted from application code.
    """

    __tablename__ = "approval_queue"
    __table_args__ = (
        Index(
            "uq_approval_queue_one_active_item",
            "is_active_item",
            unique=True,
            postgresql_where=text("is_active_item"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Only link to the analysis schema - deliberately minimal, see module
    # docstring. Not nullable: every approval item must trace back to a
    # real classification result.
    analysis_result_ref: Mapped[uuid.UUID] = mapped_column(ForeignKey("classification_results.id"), nullable=False)

    type: Mapped[ApprovalItemType] = mapped_column(
        Enum(ApprovalItemType, name="approval_item_type"),
        default=ApprovalItemType.PURCHASE_RECOMMENDATION,
        nullable=False,
    )
    status: Mapped[ApprovalStatus] = mapped_column(Enum(ApprovalStatus, name="approval_status"), nullable=False)

    # Null when this is the active item; integer order among `queued`
    # items otherwise (1 = next up). See service.py for insertion rules.
    queue_position: Mapped[int | None] = mapped_column(Integer)

    # Human-readable string OpenClaw sends verbatim to Telegram - backend
    # generates this, OpenClaw does not reformat it. Must include real
    # product detail (description/brand), not just an ASIN, per owner
    # decision - see service.py::_build_summary.
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    # Denormalized snapshot of just the fields needed to redisplay the
    # summary without a live join: asin, description, brand,
    # supplier_name, classification, roi_pct, margin_pct, unit_cost,
    # sell_price, profit. Not a full copy of the analysis row.
    summary_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # timezone=True (unlike most other timestamp columns in this codebase)
    # is deliberate here, not an inconsistency: reminder_job.py does real
    # arithmetic/comparison against a freshly-constructed
    # datetime.now(UTC) (due_at = pending_started_at + INITIAL_WAIT; now <
    # due_at), which breaks with "can't compare offset-naive and
    # offset-aware datetimes" the moment a naive-column value round-trips
    # through Postgres - found by actually running the reminder-cadence
    # tests, not assumed. Elsewhere in this codebase (e.g.
    # ProductMatch.confirmed_at, ManualReviewQueue.resolved_at) the same
    # datetime.now(UTC)-into-a-naive-column pattern is harmless because
    # those values are only ever stored and displayed, never used in
    # later time-arithmetic - this table is different.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    pending_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[ApprovalResolution | None] = mapped_column(Enum(ApprovalResolution, name="approval_resolution"))
    resolution_note: Mapped[str | None] = mapped_column(Text)

    # True only for the single currently-active item (pending/reminder_N/
    # holding) - see __table_args__ above. False for queued/terminal rows.
    is_active_item: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)


class ApprovalAuditLog(Base):
    """
    Append-only audit trail for every approve/deny/revoke call against
    approval_queue - rows here are NEVER updated or deleted, only ever
    inserted (see modules/approvals/service.py::_log_audit_event). Exists
    specifically so a revoke's `approval_revoked` event sits permanently
    next to the original `approved` event it reverses, per explicit
    owner decision (2026-10-08, revoke-path spec): approvals must be
    reversible, and the history of a reversal must be provable, not
    just inferable from the current row state.

    `approval_queue.resolution`/`resolved_at` still hold the *current*
    resolution for fast reads and existing idempotency logic; this table
    is the full history, including calls that were rejected outright
    (bad id, wrong state, conflicting replay) - "audit-log every call,
    including rejected ones" per the same owner decision.
    """

    __tablename__ = "approval_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Deliberately nullable, no FK: a rejected call against a bad/unknown
    # id still gets logged, and may not correspond to any real row.
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)

    # The raw caller-supplied id string, always recorded verbatim even
    # when it never parsed as a UUID at all - this table's whole point
    # is knowing what was *attempted*, not only what succeeded.
    requested_approval_id: Mapped[str | None] = mapped_column(Text)

    action: Mapped[str] = mapped_column(Text, nullable=False)  # "resolve" | "revoke"
    event: Mapped[ApprovalAuditEvent] = mapped_column(Enum(ApprovalAuditEvent, name="approval_audit_event"), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Caller identity - "openclaw-mcp" for every approve/deny call today
    # (no per-Telegram-user attribution is threaded through MCP yet), or
    # whoever the admin revoke endpoint's caller says they are.
    actor: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)  # resolution note / revoke reason
    error: Mapped[str | None] = mapped_column(Text)  # populated when ok=False

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
