"""
Pydantic request/response models for the tool interface.

This is deliberately the only place JSON shapes are defined for the API -
OpenClaw calls these endpoints as tools and formats the results in plain
language, but never computes anything itself (see CLAUDE.md: "OpenClaw
must never contain business logic"). Every field here is either an input
OpenClaw collected from the owner (a file, a confirmed mapping, an
approval) or a fully-computed output ready to relay as-is.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class SupplierCreate(BaseModel):
    name: str
    code: str
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    notes: str | None = None


class SupplierOut(BaseModel):
    id: uuid.UUID
    name: str
    code: str
    is_active: bool

    model_config = {"from_attributes": True}


class ListRunOut(BaseModel):
    id: uuid.UUID
    supplier_id: uuid.UUID
    source_file_original_filename: str
    status: str
    total_rows: int | None
    processed_rows: int
    error_rows: int
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_run(cls, run) -> ListRunOut:
        return cls(
            id=run.id,
            supplier_id=run.supplier_id,
            source_file_original_filename=run.source_file_original_filename,
            status=run.status.value,
            total_rows=run.total_rows,
            processed_rows=run.processed_rows,
            error_rows=run.error_rows,
            created_at=run.created_at,
        )


class IngestResponse(BaseModel):
    run: ListRunOut
    warnings: list[str]


class ProposedFieldMapping(BaseModel):
    header: str | None
    confidence: float


class ProposedMappingOut(BaseModel):
    run_id: uuid.UUID
    mapping: dict[str, ProposedFieldMapping]


class ConfirmMappingRequest(BaseModel):
    supplier_item_number: str
    description: str | None = None
    brand: str | None = None
    unit_price: str | None = None
    case_quantity: str | None = None
    case_unit: str | None = None
    confirmed_by: str


class ProcessRunQueuedResponse(BaseModel):
    """
    Returned immediately by POST /runs/{run_id}/process - the run is
    enqueued to SQS for the worker (worker.py) to actually process, not
    processed inline. Row/error/review counts aren't known yet at enqueue
    time (unlike the old synchronous response) - poll GET /runs/{run_id}
    for those once status is completed/partial.
    """

    run_id: uuid.UUID
    status: str


class LineItemResultOut(BaseModel):
    id: uuid.UUID
    row_number: int
    supplier_item_number: str | None
    description: str | None
    unit_price: Decimal | None
    item_type: str
    parse_status: str
    asin: str | None
    match_confidence: float | None
    match_status: str | None
    classification: str | None
    roi: Decimal | None
    margin: Decimal | None
    needs_review: bool


class RunResultsOut(BaseModel):
    run_id: uuid.UUID
    items: list[LineItemResultOut]


class ReviewQueueEntryOut(BaseModel):
    id: uuid.UUID
    raw_line_item_id: uuid.UUID
    reason: str
    reason_notes: str | None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ResolveReviewRequest(BaseModel):
    resolved_by: str
    notes: str | None = None


class ConfirmMatchRequest(BaseModel):
    confirmed_by: str
    asin: str | None = None  # override - if different from the proposed ASIN


class RejectMatchRequest(BaseModel):
    confirmed_by: str


class MatchOut(BaseModel):
    id: uuid.UUID
    supplier_item_number: str
    asin: str | None
    match_confidence: float | None
    match_status: str
    match_source: str

    model_config = {"from_attributes": True}


class RunComparisonRow(BaseModel):
    supplier_item_number: str
    run_a_classification: str | None
    run_a_roi: Decimal | None
    run_b_classification: str | None
    run_b_roi: Decimal | None
    changed: bool


class RunComparisonOut(BaseModel):
    run_a_id: uuid.UUID
    run_b_id: uuid.UUID
    rows: list[RunComparisonRow]
