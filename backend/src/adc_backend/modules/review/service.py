"""Review queue orchestration: builds LineItemReviewState from the DB, routes it, persists the queue entry."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from adc_backend.modules.ingestion.models import RawLineItem
from adc_backend.modules.matching.models import ProductMatch
from adc_backend.modules.review.models import ManualReviewQueue, ReviewStatus
from adc_backend.modules.review.routing import LineItemReviewState, determine_review_reason
from adc_backend.modules.rules.models import ClassificationResult


class ReviewServiceError(Exception):
    pass


def evaluate_and_route(db: Session, list_run_id: uuid.UUID, raw_line_item_id: uuid.UUID) -> ManualReviewQueue | None:
    """
    Returns the queue entry if this item needs review, else None (and
    removes nothing - see module note on not reopening resolved entries).
    """
    item = db.get(RawLineItem, raw_line_item_id)
    if item is None:
        raise ReviewServiceError(f"No raw_line_item with id {raw_line_item_id}")

    match = None
    if item.product_match_id is not None:
        match = db.get(ProductMatch, item.product_match_id)

    classification_result = db.execute(
        select(ClassificationResult).where(ClassificationResult.raw_line_item_id == raw_line_item_id)
    ).scalar_one_or_none()

    state = LineItemReviewState(
        item_type=item.item_type,
        parse_status=item.parse_status,
        match_status=match.match_status if match else None,
        match_confidence=float(match.match_confidence) if match and match.match_confidence is not None else None,
        classification=classification_result.classification if classification_result else None,
        classification_trace=classification_result.rule_trace if classification_result else None,
    )

    outcome = determine_review_reason(state)

    existing = db.execute(
        select(ManualReviewQueue).where(ManualReviewQueue.raw_line_item_id == raw_line_item_id)
    ).scalar_one_or_none()

    if outcome is None:
        return None  # not flagged (or no longer flagged) - existing resolved/dismissed entries are left as history

    reason, notes = outcome

    if existing is not None:
        if existing.status != ReviewStatus.PENDING:
            # Owner already resolved or dismissed this - a re-run (e.g. new
            # live data) doesn't silently reopen their decision.
            return existing
        existing.reason = reason
        existing.reason_notes = notes
        db.flush()
        return existing

    entry = ManualReviewQueue(list_run_id=list_run_id, raw_line_item_id=raw_line_item_id, reason=reason, reason_notes=notes)
    db.add(entry)
    db.flush()
    return entry


def resolve_review_entry(db: Session, entry_id: uuid.UUID, resolved_by: str, notes: str | None = None) -> ManualReviewQueue:
    entry = db.get(ManualReviewQueue, entry_id)
    if entry is None:
        raise ReviewServiceError(f"No manual_review_queue entry with id {entry_id}")
    entry.status = ReviewStatus.RESOLVED
    entry.resolved_by = resolved_by
    entry.resolved_at = datetime.now(UTC)
    entry.resolution_notes = notes
    db.flush()
    return entry


def dismiss_review_entry(db: Session, entry_id: uuid.UUID, resolved_by: str, notes: str | None = None) -> ManualReviewQueue:
    entry = db.get(ManualReviewQueue, entry_id)
    if entry is None:
        raise ReviewServiceError(f"No manual_review_queue entry with id {entry_id}")
    entry.status = ReviewStatus.DISMISSED
    entry.resolved_by = resolved_by
    entry.resolved_at = datetime.now(UTC)
    entry.resolution_notes = notes
    db.flush()
    return entry
