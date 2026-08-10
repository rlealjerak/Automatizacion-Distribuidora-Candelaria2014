"""
End-to-end tests through the actual FastAPI router (TestClient), against
real local Postgres + real S3 - the router wiring itself (request/response
schemas, dependency injection, status codes) hasn't been exercised
anywhere else; every other test file goes straight at the service layer.

Deliberately does NOT call POST /runs/{id}/process here - that endpoint
calls real SPAPIClient()/KeepaClient() instances, which would attempt
actual SP-API/Keepa network calls against placeholder credentials (see
sp_api_client.py's module docstring) rather than a controlled test
double. Steps that don't need live external APIs (upload, mapping,
results/export/review-queue/match endpoints against manually-seeded data)
are tested directly; process_run's own orchestration logic is covered
separately in test_orchestration.py against stub clients.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL") or not os.environ.get("S3_BUCKET_NAME"),
    reason="DATABASE_URL and S3_BUCKET_NAME must both be set",
)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from adc_backend.main import app

    return TestClient(app)


@pytest.fixture
def db_session():
    from adc_backend.db import models  # noqa: F401
    from adc_backend.db.base import get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def _s3_cleanup():
    keys: list[str] = []
    yield keys
    if keys:
        import boto3

        s3 = boto3.client("s3")
        for key in keys:
            s3.delete_object(Bucket=os.environ["S3_BUCKET_NAME"], Key=key)


def test_supplier_upload_mapping_flow(client, _s3_cleanup):
    # 1. Create a supplier.
    supplier_resp = client.post(
        "/suppliers",
        json={"name": f"API Test Supplier {uuid.uuid4()}", "code": f"api-test-{uuid.uuid4().hex[:8]}"},
    )
    assert supplier_resp.status_code == 200, supplier_resp.text
    supplier_id = supplier_resp.json()["id"]

    # 2. Upload a CSV list.
    csv_content = b"ITEM #,DESCRIPTION,SALE PRICE\nABC-1,Blue Widget,2.90\n"
    upload_resp = client.post(
        f"/runs?supplier_id={supplier_id}",
        files={"file": ("test-list.csv", csv_content, "text/csv")},
    )
    assert upload_resp.status_code == 200, upload_resp.text
    body = upload_resp.json()
    run_id = body["run"]["id"]
    assert body["run"]["status"] == "mapping_pending"
    assert body["run"]["total_rows"] == 1

    # Track the S3 object for cleanup - GET /runs/{id} doesn't expose the
    # s3 key, so reconstruct it the way ingestion does.
    _s3_cleanup.append(f"sources/{supplier_resp.json()['code']}/{run_id}/test-list.csv")

    # 3. Fetch the run and confirm status is queryable.
    get_resp = client.get(f"/runs/{run_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == run_id

    # 4. Get the proposed column mapping.
    mapping_resp = client.get(f"/runs/{run_id}/mapping")
    assert mapping_resp.status_code == 200
    mapping = mapping_resp.json()["mapping"]
    assert mapping["supplier_item_number"]["header"] == "ITEM #"
    assert mapping["unit_price"]["header"] == "SALE PRICE"

    # 5. Confirm the mapping.
    confirm_resp = client.post(
        f"/runs/{run_id}/mapping/confirm",
        json={
            "supplier_item_number": "ITEM #",
            "description": "DESCRIPTION",
            "unit_price": "SALE PRICE",
            "confirmed_by": "test-owner",
        },
    )
    assert confirm_resp.status_code == 200, confirm_resp.text
    assert confirm_resp.json()["status"] == "mapping_confirmed"

    # 6. List runs for the supplier.
    list_resp = client.get(f"/suppliers/{supplier_id}/runs")
    assert list_resp.status_code == 200
    assert any(r["id"] == run_id for r in list_resp.json())


def test_confirm_mapping_rejects_missing_item_number(client, _s3_cleanup):
    supplier_resp = client.post(
        "/suppliers", json={"name": f"API Test Supplier {uuid.uuid4()}", "code": f"api-test-{uuid.uuid4().hex[:8]}"}
    )
    supplier_id = supplier_resp.json()["id"]
    upload_resp = client.post(
        f"/runs?supplier_id={supplier_id}",
        files={"file": ("test.csv", b"A,B\n1,2\n", "text/csv")},
    )
    run_id = upload_resp.json()["run"]["id"]
    _s3_cleanup.append(f"sources/{supplier_resp.json()['code']}/{run_id}/test.csv")

    resp = client.post(f"/runs/{run_id}/mapping/confirm", json={"supplier_item_number": "", "confirmed_by": "test"})
    assert resp.status_code == 400


def test_results_export_and_review_endpoints_against_seeded_data(client, db_session):
    from adc_backend.db.core_models import ListRun, SourceFileType, Supplier
    from adc_backend.modules.ingestion.models import RawLineItem
    from adc_backend.modules.review.models import ManualReviewQueue, ReviewReason
    from adc_backend.modules.rules.models import ClassificationLabel, ClassificationResult

    supplier = Supplier(name=f"Seed Supplier {uuid.uuid4()}", code=f"seed-{uuid.uuid4().hex[:8]}")
    db_session.add(supplier)
    db_session.flush()
    run = ListRun(
        supplier_id=supplier.id,
        source_file_s3_key="s3://x/y.csv",
        source_file_original_filename="y.csv",
        source_file_type=SourceFileType.CSV,
    )
    db_session.add(run)
    db_session.flush()
    item = RawLineItem(list_run_id=run.id, row_number=1, raw_data={}, supplier_item_number="ABC-1", unit_price=Decimal(10))
    db_session.add(item)
    db_session.flush()
    db_session.add(
        ClassificationResult(
            list_run_id=run.id,
            raw_line_item_id=item.id,
            classification=ClassificationLabel.BUY,
            roi=Decimal(50),
            margin=Decimal(25),
            rule_trace=[{"rule": "test", "result": "info", "reasoning": "seeded"}],
        )
    )
    review_entry = ManualReviewQueue(list_run_id=run.id, raw_line_item_id=item.id, reason=ReviewReason.OTHER, reason_notes="seeded")
    db_session.add(review_entry)
    db_session.commit()  # the router uses its own session/connection - must actually commit for it to see this

    try:
        results_resp = client.get(f"/runs/{run.id}/results")
        assert results_resp.status_code == 200
        items = results_resp.json()["items"]
        assert len(items) == 1
        assert items[0]["classification"] == "buy"
        assert items[0]["needs_review"] is True

        export_resp = client.get(f"/runs/{run.id}/export")
        assert export_resp.status_code == 200
        assert b"ABC-1" in export_resp.content

        queue_resp = client.get(f"/runs/{run.id}/review-queue")
        assert queue_resp.status_code == 200
        assert len(queue_resp.json()) == 1

        resolve_resp = client.post(f"/review/{review_entry.id}/resolve", json={"resolved_by": "test-owner"})
        assert resolve_resp.status_code == 200
        assert resolve_resp.json()["status"] == "resolved"
    finally:
        # Clean up committed rows so this disposable local DB doesn't
        # accumulate test data indefinitely across runs.
        db_session.query(ManualReviewQueue).filter_by(list_run_id=run.id).delete()
        db_session.query(ClassificationResult).filter_by(list_run_id=run.id).delete()
        db_session.query(RawLineItem).filter_by(list_run_id=run.id).delete()
        db_session.query(ListRun).filter_by(id=run.id).delete()
        db_session.query(Supplier).filter_by(id=supplier.id).delete()
        db_session.commit()


def test_match_confirm_and_reject_endpoints(client, db_session):
    from adc_backend.db.core_models import Supplier
    from adc_backend.modules.matching.models import MatchSource, MatchStatus, ProductMatch

    supplier = Supplier(name=f"Seed Supplier {uuid.uuid4()}", code=f"seed-{uuid.uuid4().hex[:8]}")
    db_session.add(supplier)
    db_session.flush()
    match = ProductMatch(
        supplier_id=supplier.id,
        supplier_item_number="ABC-1",
        asin="B000000001",
        match_confidence=Decimal("0.9"),
        match_status=MatchStatus.PROPOSED,
        match_source=MatchSource.AUTO,
    )
    db_session.add(match)
    db_session.commit()

    try:
        confirm_resp = client.post(f"/matches/{match.id}/confirm", json={"confirmed_by": "test-owner"})
        assert confirm_resp.status_code == 200
        assert confirm_resp.json()["match_status"] == "confirmed"

        match2 = ProductMatch(
            supplier_id=supplier.id,
            supplier_item_number="ABC-2",
            asin="B000000002",
            match_status=MatchStatus.PROPOSED,
            match_source=MatchSource.AUTO,
        )
        db_session.add(match2)
        db_session.commit()

        reject_resp = client.post(f"/matches/{match2.id}/reject", json={"confirmed_by": "test-owner"})
        assert reject_resp.status_code == 200
        assert reject_resp.json()["match_status"] == "rejected"
    finally:
        db_session.query(ProductMatch).filter_by(supplier_id=supplier.id).delete()
        db_session.query(Supplier).filter_by(id=supplier.id).delete()
        db_session.commit()
