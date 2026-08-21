"""
OpenClaw-facing tool interface - build-order step 10.

Every endpoint here either takes something OpenClaw collected from the
owner (a file, a confirmed mapping, an approval decision) or returns
something already fully computed for OpenClaw to relay in plain language.
No endpoint performs matching/financial/rule logic inline - it all goes
through the module functions built in steps 3-9, so this file stays a
thin translation layer, not a place business logic could quietly leak
into (see CLAUDE.md: "if you find yourself writing matching or
calculation logic that OpenClaw would call directly instead of this
backend, stop and flag it" - the same discipline applies one layer in,
to this router).
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from adc_backend.config import get_settings, get_sqs_client
from adc_backend.db.base import get_db
from adc_backend.db.core_models import ListRun, Supplier
from adc_backend.modules.ingestion.models import RawLineItem
from adc_backend.modules.ingestion.service import IngestionError, ingest_file
from adc_backend.modules.matching.engine import confirm_match, reject_match
from adc_backend.modules.matching.models import ProductMatch
from adc_backend.modules.normalization.service import (
    ConfirmedMapping,
    NormalizationError,
    confirm_mapping,
    propose_mapping_for_run,
)
from adc_backend.modules.review.models import ManualReviewQueue, ReviewStatus
from adc_backend.modules.review.service import (
    ReviewServiceError,
    dismiss_review_entry,
    resolve_review_entry,
)
from adc_backend.modules.rules.models import ClassificationResult
from adc_backend.modules.tools.export import compare_runs, export_run_results_csv
from adc_backend.modules.tools.schemas import (
    ConfirmMappingRequest,
    ConfirmMatchRequest,
    IngestResponse,
    LineItemResultOut,
    ListRunOut,
    MatchOut,
    ProcessRunQueuedResponse,
    ProposedFieldMapping,
    ProposedMappingOut,
    RejectMatchRequest,
    ResolveReviewRequest,
    RunComparisonOut,
    RunComparisonRow,
    RunResultsOut,
    SupplierCreate,
    SupplierOut,
)

router = APIRouter()


# --- suppliers ---


@router.post("/suppliers", response_model=SupplierOut)
def create_supplier(body: SupplierCreate, db: Session = Depends(get_db)):
    supplier = Supplier(**body.model_dump())
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return supplier


@router.get("/suppliers", response_model=list[SupplierOut])
def list_suppliers(db: Session = Depends(get_db)):
    return db.execute(select(Supplier).order_by(Supplier.name)).scalars().all()


# --- ingestion (step 3) ---


@router.post("/runs", response_model=IngestResponse)
async def upload_list(supplier_id: uuid.UUID, file: UploadFile, db: Session = Depends(get_db)):
    content = await file.read()
    try:
        outcome = ingest_file(db, supplier_id=supplier_id, original_filename=file.filename, content=content)
    except IngestionError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    db.commit()
    return IngestResponse(run=ListRunOut.from_orm_run(outcome.run), warnings=outcome.warnings)


@router.get("/runs/{run_id}", response_model=ListRunOut)
def get_run(run_id: uuid.UUID, db: Session = Depends(get_db)):
    run = db.get(ListRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return ListRunOut.from_orm_run(run)


@router.get("/suppliers/{supplier_id}/runs", response_model=list[ListRunOut])
def list_supplier_runs(supplier_id: uuid.UUID, db: Session = Depends(get_db)):
    runs = db.execute(select(ListRun).where(ListRun.supplier_id == supplier_id).order_by(ListRun.created_at.desc())).scalars().all()
    return [ListRunOut.from_orm_run(r) for r in runs]


# --- normalization / column mapping (step 4) ---


@router.get("/runs/{run_id}/mapping", response_model=ProposedMappingOut)
def get_proposed_mapping(run_id: uuid.UUID, db: Session = Depends(get_db)):
    try:
        run = propose_mapping_for_run(db, run_id)
    except NormalizationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    db.commit()
    mapping = {field: ProposedFieldMapping(**value) for field, value in run.proposed_column_mapping.items()}
    return ProposedMappingOut(run_id=run_id, mapping=mapping)


@router.post("/runs/{run_id}/mapping/confirm", response_model=ListRunOut)
def confirm_run_mapping(run_id: uuid.UUID, body: ConfirmMappingRequest, db: Session = Depends(get_db)):
    mapping = ConfirmedMapping(
        supplier_item_number=body.supplier_item_number,
        description=body.description,
        brand=body.brand,
        unit_price=body.unit_price,
        case_quantity=body.case_quantity,
        case_unit=body.case_unit,
    )
    try:
        run = confirm_mapping(db, run_id, mapping, confirmed_by=body.confirmed_by)
    except NormalizationError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    db.commit()
    return ListRunOut.from_orm_run(run)


# --- processing (steps 5-9 combined: matching, live data, classification, review routing) ---


@router.post("/runs/{run_id}/process", response_model=ProcessRunQueuedResponse, status_code=202)
def process_run_endpoint(run_id: uuid.UUID, db: Session = Depends(get_db)):
    """
    Enqueues the run to SQS (`adc-prod-list-processing`) for the worker
    (worker.py, deployed as its own ECS service) to actually process -
    CLAUDE.md requires list processing to run as a background job, not
    inline in a request: a real 5,000+ row list risks an ALB timeout on
    the synchronous path, and a mid-request crash would lose all
    progress with no retry. Fails fast here, before enqueueing, if the
    run doesn't exist or has no confirmed mapping yet, so the caller
    gets an immediate 400 instead of a message that would just dead-letter.

    Was a synchronous trigger before the worker had anywhere to run
    (see git history) - now that it's deployed, this always enqueues.
    """
    run = db.get(ListRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.confirmed_column_mapping is None:
        raise HTTPException(status_code=400, detail=f"list_run {run_id} has no confirmed column mapping - can't process")

    settings = get_settings()
    get_sqs_client().send_message(
        QueueUrl=settings.sqs_queue_url,
        MessageBody=json.dumps({"run_id": str(run_id)}),
    )
    return ProcessRunQueuedResponse(run_id=run_id, status=run.status.value)


# --- results (owner review surface) ---


@router.get("/runs/{run_id}/results", response_model=RunResultsOut)
def get_run_results(run_id: uuid.UUID, classification: str | None = None, db: Session = Depends(get_db)):
    items = db.execute(select(RawLineItem).where(RawLineItem.list_run_id == run_id).order_by(RawLineItem.row_number)).scalars().all()
    match_ids = [i.product_match_id for i in items if i.product_match_id]
    matches = {m.id: m for m in db.execute(select(ProductMatch).where(ProductMatch.id.in_(match_ids))).scalars()} if match_ids else {}
    results = {
        r.raw_line_item_id: r for r in db.execute(select(ClassificationResult).where(ClassificationResult.list_run_id == run_id)).scalars()
    }
    review_ids = {
        r.raw_line_item_id
        for r in db.execute(
            select(ManualReviewQueue).where(ManualReviewQueue.list_run_id == run_id, ManualReviewQueue.status == ReviewStatus.PENDING)
        ).scalars()
    }

    out_items = []
    for item in items:
        match = matches.get(item.product_match_id) if item.product_match_id else None
        result = results.get(item.id)
        if classification and (result is None or result.classification.value != classification.lower()):
            continue
        out_items.append(
            LineItemResultOut(
                id=item.id,
                row_number=item.row_number,
                supplier_item_number=item.supplier_item_number,
                description=item.description,
                unit_price=item.unit_price,
                item_type=item.item_type.value,
                parse_status=item.parse_status.value,
                asin=match.asin if match else None,
                match_confidence=float(match.match_confidence) if match and match.match_confidence is not None else None,
                match_status=match.match_status.value if match else None,
                classification=result.classification.value if result else None,
                roi=result.roi if result else None,
                margin=result.margin if result else None,
                needs_review=item.id in review_ids,
            )
        )
    return RunResultsOut(run_id=run_id, items=out_items)


@router.get("/runs/{run_id}/export")
def export_run(run_id: uuid.UUID, classification: str | None = None, db: Session = Depends(get_db)):
    filter_list = [c.strip() for c in classification.split(",")] if classification else None
    csv_bytes = export_run_results_csv(db, run_id, filter_list)
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="run-{run_id}-results.csv"'},
    )


@router.get("/runs/compare", response_model=RunComparisonOut)
def compare_two_runs(run_a: uuid.UUID, run_b: uuid.UUID, db: Session = Depends(get_db)):
    rows = compare_runs(db, run_a, run_b)
    return RunComparisonOut(run_a_id=run_a, run_b_id=run_b, rows=[RunComparisonRow(**r) for r in rows])


# --- manual review queue (step 9) ---


@router.get("/runs/{run_id}/review-queue", response_model=list[dict])
def get_review_queue(run_id: uuid.UUID, db: Session = Depends(get_db)):
    entries = db.execute(
        select(ManualReviewQueue).where(ManualReviewQueue.list_run_id == run_id, ManualReviewQueue.status == ReviewStatus.PENDING)
    ).scalars().all()
    return [
        {
            "id": str(e.id),
            "raw_line_item_id": str(e.raw_line_item_id),
            "reason": e.reason.value,
            "reason_notes": e.reason_notes,
            "status": e.status.value,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]


@router.post("/review/{entry_id}/resolve")
def resolve_review(entry_id: uuid.UUID, body: ResolveReviewRequest, db: Session = Depends(get_db)):
    try:
        entry = resolve_review_entry(db, entry_id, resolved_by=body.resolved_by, notes=body.notes)
    except ReviewServiceError as e:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(e)) from e
    db.commit()
    return {"id": str(entry.id), "status": entry.status.value}


@router.post("/review/{entry_id}/dismiss")
def dismiss_review(entry_id: uuid.UUID, body: ResolveReviewRequest, db: Session = Depends(get_db)):
    try:
        entry = dismiss_review_entry(db, entry_id, resolved_by=body.resolved_by, notes=body.notes)
    except ReviewServiceError as e:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(e)) from e
    db.commit()
    return {"id": str(entry.id), "status": entry.status.value}


# --- product matches (step 7 confirmation) ---


@router.post("/matches/{match_id}/confirm", response_model=MatchOut)
def confirm_match_endpoint(match_id: uuid.UUID, body: ConfirmMatchRequest, db: Session = Depends(get_db)):
    try:
        match = confirm_match(db, match_id, confirmed_by=body.confirmed_by, asin=body.asin)
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(e)) from e
    db.commit()
    return match


@router.post("/matches/{match_id}/reject", response_model=MatchOut)
def reject_match_endpoint(match_id: uuid.UUID, body: RejectMatchRequest, db: Session = Depends(get_db)):
    try:
        match = reject_match(db, match_id, confirmed_by=body.confirmed_by)
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(e)) from e
    db.commit()
    return match
