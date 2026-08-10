"""
Normalization orchestration: propose a column mapping for a run, let the
owner confirm/correct it, then apply it to every raw line item - filling
in the typed columns, resolving case units, and classifying item_type.
Nothing here runs automatically past the confirmation step (build-order
step 4's explicit requirement: "Allow the owner to confirm/correct column
mapping before processing").
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from adc_backend.db.core_models import ListRun, ListRunStatus
from adc_backend.modules.ingestion.models import RawLineItem
from adc_backend.modules.normalization.classify_line_item import classify_line_item
from adc_backend.modules.normalization.column_mapping import mapping_to_json, propose_column_mapping
from adc_backend.modules.normalization.unit_lookup import resolve_unit


class NormalizationError(Exception):
    pass


def propose_mapping_for_run(db: Session, run_id: uuid.UUID) -> ListRun:
    run = db.get(ListRun, run_id)
    if run is None:
        raise NormalizationError(f"No list_run with id {run_id}")

    sample_row = db.execute(
        select(RawLineItem).where(RawLineItem.list_run_id == run_id).order_by(RawLineItem.row_number).limit(1)
    ).scalar_one_or_none()
    if sample_row is None:
        raise NormalizationError(f"list_run {run_id} has no raw line items to propose a mapping from")

    headers = list(sample_row.raw_data.keys())
    mapping = propose_column_mapping(headers)
    run.proposed_column_mapping = mapping_to_json(mapping)
    db.flush()
    return run


@dataclass
class ConfirmedMapping:
    """What the owner actually approved - one source header (or None) per canonical field."""

    supplier_item_number: str | None
    description: str | None
    brand: str | None
    unit_price: str | None
    case_quantity: str | None
    case_unit: str | None


def confirm_mapping(db: Session, run_id: uuid.UUID, mapping: ConfirmedMapping, confirmed_by: str) -> ListRun:
    run = db.get(ListRun, run_id)
    if run is None:
        raise NormalizationError(f"No list_run with id {run_id}")
    if not mapping.supplier_item_number:
        # Every downstream step (persistent matching, in particular) keys off
        # this - refuse to confirm a mapping that can't identify products at all.
        raise NormalizationError("confirmed mapping must include supplier_item_number")

    run.confirmed_column_mapping = {
        "supplier_item_number": mapping.supplier_item_number,
        "description": mapping.description,
        "brand": mapping.brand,
        "unit_price": mapping.unit_price,
        "case_quantity": mapping.case_quantity,
        "case_unit": mapping.case_unit,
    }
    run.column_mapping_confirmed_at = datetime.now(UTC)
    run.column_mapping_confirmed_by = confirmed_by
    run.status = ListRunStatus.MAPPING_CONFIRMED
    db.flush()
    return run


def apply_normalization(db: Session, run_id: uuid.UUID) -> dict[str, int]:
    """
    Applies the confirmed mapping to every raw line item in the run.
    Per-row errors don't stop the run (row isolation, per CLAUDE.md
    acceptance criteria) - each row gets its own parse_status/item_type
    instead. Returns a small summary count for the caller to report.
    """
    run = db.get(ListRun, run_id)
    if run is None:
        raise NormalizationError(f"No list_run with id {run_id}")
    if run.confirmed_column_mapping is None:
        raise NormalizationError(f"list_run {run_id} has no confirmed column mapping yet")

    mapping = run.confirmed_column_mapping
    run.status = ListRunStatus.PROCESSING
    db.flush()

    items = db.execute(select(RawLineItem).where(RawLineItem.list_run_id == run_id)).scalars().all()

    counts = {"ok": 0, "needs_review": 0, "error": 0}
    for item in items:
        raw = item.raw_data
        supplier_item_number = _get(raw, mapping.get("supplier_item_number"))
        description = _get(raw, mapping.get("description"))
        brand = _get(raw, mapping.get("brand"))
        raw_price_text = _get(raw, mapping.get("unit_price"))
        raw_case_qty_text = _get(raw, mapping.get("case_quantity"))
        raw_case_unit_text = _get(raw, mapping.get("case_unit"))

        outcome = classify_line_item(
            description=description,
            raw_price_text=raw_price_text,
            supplier_item_number=supplier_item_number,
        )

        item.supplier_item_number = supplier_item_number
        item.description = description
        item.brand = brand
        item.unit_price = outcome.unit_price
        item.case_unit_raw = raw_case_unit_text
        item.item_type = outcome.item_type
        item.parse_status = outcome.parse_status
        item.parse_notes = outcome.notes

        if raw_case_qty_text:
            try:
                item.case_quantity = _parse_decimal(raw_case_qty_text)
            except ValueError:
                pass  # leave null - not critical enough to flip parse_status over on its own

        unit_row = resolve_unit(db, raw_case_unit_text)
        if unit_row is not None:
            item.case_unit_normalized = unit_row.normalized_unit

        counts[outcome.parse_status.value] += 1

    run.error_rows = counts["error"]
    run.processed_rows = len(items)
    db.flush()

    return counts


def _get(raw_data: dict, header: str | None) -> str | None:
    if not header:
        return None
    value = raw_data.get(header)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_decimal(text: str) -> Decimal:
    try:
        return Decimal(text.replace(",", "").strip())
    except InvalidOperation as e:
        raise ValueError(f"not a number: {text!r}") from e
