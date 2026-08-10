"""
Export and run-history/comparison - build-order step 11.

Export never touches the original source file (that stays immutable in
S3, untouched, per CLAUDE.md) - this generates a new derived CSV from the
database at request time. Comparison joins two runs from the same
supplier by supplier_item_number, which is exactly the persistent
matching key this system already keys `product_matches` on (build-order
step 2/7) - reusing it here means comparison "just works" for any two
runs, not something bolted on separately per supplier.
"""

from __future__ import annotations

import csv
import io
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from adc_backend.db.core_models import ListRun
from adc_backend.modules.ingestion.models import RawLineItem
from adc_backend.modules.matching.models import ProductMatch
from adc_backend.modules.rules.models import ClassificationResult

EXPORT_COLUMNS = [
    "row_number",
    "supplier_item_number",
    "description",
    "unit_price",
    "item_type",
    "parse_status",
    "asin",
    "match_confidence",
    "match_status",
    "classification",
    "roi",
    "margin",
]


def export_run_results_csv(db: Session, run_id: uuid.UUID, classification_filter: list[str] | None = None) -> bytes:
    rows = _run_result_rows(db, run_id)
    if classification_filter:
        allowed = {c.lower() for c in classification_filter}
        rows = [r for r in rows if r["classification"] and r["classification"].lower() in allowed]

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in EXPORT_COLUMNS})
    return buffer.getvalue().encode("utf-8")


def _run_result_rows(db: Session, run_id: uuid.UUID) -> list[dict]:
    items = db.execute(select(RawLineItem).where(RawLineItem.list_run_id == run_id)).scalars().all()
    match_ids = [i.product_match_id for i in items if i.product_match_id]
    matches = {m.id: m for m in db.execute(select(ProductMatch).where(ProductMatch.id.in_(match_ids))).scalars()} if match_ids else {}
    results = {
        r.raw_line_item_id: r
        for r in db.execute(select(ClassificationResult).where(ClassificationResult.list_run_id == run_id)).scalars()
    }

    rows = []
    for item in items:
        match = matches.get(item.product_match_id) if item.product_match_id else None
        result = results.get(item.id)
        rows.append(
            {
                "row_number": item.row_number,
                "supplier_item_number": item.supplier_item_number or "",
                "description": item.description or "",
                "unit_price": str(item.unit_price) if item.unit_price is not None else "",
                "item_type": item.item_type.value,
                "parse_status": item.parse_status.value,
                "asin": match.asin if match else "",
                "match_confidence": str(match.match_confidence) if match and match.match_confidence is not None else "",
                "match_status": match.match_status.value if match else "",
                "classification": result.classification.value if result else "",
                "roi": str(result.roi) if result and result.roi is not None else "",
                "margin": str(result.margin) if result and result.margin is not None else "",
            }
        )
    return rows


def compare_runs(db: Session, run_a_id: uuid.UUID, run_b_id: uuid.UUID) -> list[dict]:
    """
    One row per supplier_item_number seen in either run, showing how its
    classification/ROI changed. CLAUDE.md acceptance criteria: "support
    comparing different lists from the same supplier over time" - this
    doesn't enforce same-supplier (a caller could compare across
    suppliers by mistake), so it's on the caller/API layer to pass two
    runs that make sense to compare.
    """
    rows_a = {r["supplier_item_number"]: r for r in _run_result_rows(db, run_a_id) if r["supplier_item_number"]}
    rows_b = {r["supplier_item_number"]: r for r in _run_result_rows(db, run_b_id) if r["supplier_item_number"]}

    all_item_numbers = sorted(set(rows_a) | set(rows_b))
    comparison = []
    for item_number in all_item_numbers:
        a = rows_a.get(item_number)
        b = rows_b.get(item_number)
        a_classification = a["classification"] if a else None
        b_classification = b["classification"] if b else None
        comparison.append(
            {
                "supplier_item_number": item_number,
                "run_a_classification": a_classification or None,
                "run_a_roi": a["roi"] if a and a["roi"] else None,
                "run_b_classification": b_classification or None,
                "run_b_roi": b["roi"] if b and b["roi"] else None,
                "changed": a_classification != b_classification,
            }
        )
    return comparison


def get_run_or_none(db: Session, run_id: uuid.UUID) -> ListRun | None:
    return db.get(ListRun, run_id)
