"""
Ingestion orchestration: upload preservation + raw parsing into
`raw_line_items`. Deliberately stops there - normalization (populating the
typed columns, classifying item_type, unit lookup) is build-order step 4
and operates on the rows this creates, not inline here. Keeping the two
separate means a normalization bug is fixable and re-runnable without
re-uploading/re-parsing the source file.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from adc_backend.db.core_models import ListRun, ListRunStatus, Supplier
from adc_backend.modules.ingestion.models import RawLineItem
from adc_backend.modules.ingestion.parsers import ParseResult, detect_file_type, parse_file
from adc_backend.modules.ingestion.storage import build_source_key, upload_source_file


class IngestionError(Exception):
    """Run-level ingestion failure (bad file, unreadable format, etc.)."""


@dataclass
class IngestionOutcome:
    run: ListRun
    warnings: list[str]  # parser-level notes (multi-sheet workbook, undetected PDF pages, ...) - not row errors


def ingest_file(
    db: Session,
    *,
    supplier_id: uuid.UUID,
    original_filename: str,
    content: bytes,
) -> IngestionOutcome:
    """
    Preserve the original file to S3, parse it, and create the ListRun +
    RawLineItem rows. Per-row parsing problems don't fail the run (row
    isolation, per CLAUDE.md acceptance criteria) - only a file-level
    failure (can't detect type, zero rows found) does.
    """
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise IngestionError(f"No supplier with id {supplier_id}")

    try:
        file_type = detect_file_type(original_filename)
    except ValueError as e:
        raise IngestionError(str(e)) from e

    run = ListRun(
        supplier_id=supplier_id,
        source_file_s3_key="",  # filled in below once we know the run's id
        source_file_original_filename=original_filename,
        source_file_type=file_type,
        status=ListRunStatus.UPLOADED,
    )
    db.add(run)
    db.flush()  # assigns run.id without committing - needed for the S3 key

    s3_key = build_source_key(supplier.code, run.id, original_filename)
    upload_source_file(s3_key, content)
    run.source_file_s3_key = s3_key

    try:
        result: ParseResult = parse_file(file_type, content)
    except Exception as e:
        run.status = ListRunStatus.FAILED
        run.error_summary = f"Parsing failed: {e}"
        db.flush()
        raise IngestionError(f"Parsing failed: {e}") from e

    if not result.rows:
        run.status = ListRunStatus.FAILED
        run.error_summary = "No data rows found" + (f" ({'; '.join(result.warnings)})" if result.warnings else "")
        db.flush()
        raise IngestionError(run.error_summary)

    for parsed_row in result.rows:
        db.add(RawLineItem(list_run_id=run.id, row_number=parsed_row.row_number, raw_data=parsed_row.raw_data))

    run.total_rows = len(result.rows)
    run.status = ListRunStatus.MAPPING_PENDING
    db.flush()

    return IngestionOutcome(run=run, warnings=result.warnings)
