"""
End-to-end ingestion test: real local Postgres (see test_db_models.py) +
real S3 (the actual `adc-prod-supplier-files` bucket applied in
infra/ - see docs/decisions/0003-infra-apply-findings.md). Skips if either
isn't configured, same pattern as the DB tests - this is how ingestion was
actually verified, not just asserted to work.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL") or not os.environ.get("S3_BUCKET_NAME"),
    reason="DATABASE_URL and S3_BUCKET_NAME must both be set - needs local Postgres and real S3 access",
)


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
    """
    Tests below hit the real S3 bucket (see module docstring) - the upload
    isn't part of the DB transaction so db_session's rollback doesn't
    remove it. Track keys created during the test and delete them
    afterward so repeated test runs don't leave permanent clutter in real
    production infra.
    """
    keys: list[str] = []
    yield keys
    if keys:
        import boto3

        client = boto3.client("s3")
        for key in keys:
            client.delete_object(Bucket=os.environ["S3_BUCKET_NAME"], Key=key)


def test_ingest_csv_creates_run_and_line_items_and_preserves_original_in_s3(db_session, _s3_cleanup):
    from adc_backend.db.core_models import ListRunStatus, Supplier
    from adc_backend.modules.ingestion.models import RawLineItem
    from adc_backend.modules.ingestion.service import ingest_file
    from adc_backend.modules.ingestion.storage import download_source_file

    supplier = Supplier(name=f"Test Supplier {uuid.uuid4()}", code=f"test-{uuid.uuid4().hex[:8]}")
    db_session.add(supplier)
    db_session.flush()

    content = b"ITEM #,UNIT PRICE\nABC-1,2.90\nABC-2,1.45\n"
    outcome = ingest_file(
        db_session,
        supplier_id=supplier.id,
        original_filename="test-list.csv",
        content=content,
    )
    _s3_cleanup.append(outcome.run.source_file_s3_key)

    assert outcome.run.status == ListRunStatus.MAPPING_PENDING
    assert outcome.run.total_rows == 2
    assert outcome.warnings == []

    items = db_session.query(RawLineItem).filter_by(list_run_id=outcome.run.id).order_by(RawLineItem.row_number).all()
    assert len(items) == 2
    assert items[0].raw_data == {"ITEM #": "ABC-1", "UNIT PRICE": "2.90"}

    # The original file must actually be retrievable, byte-for-byte, from
    # S3 - not just recorded as "uploaded". This is the immutability
    # guarantee CLAUDE.md requires; verify it for real, not just trust the
    # PUT call didn't raise.
    fetched = download_source_file(outcome.run.source_file_s3_key)
    assert fetched == content


def test_ingest_file_with_no_data_rows_fails_run_not_silently(db_session, _s3_cleanup):
    from adc_backend.db.core_models import Supplier
    from adc_backend.modules.ingestion.service import IngestionError, ingest_file

    supplier = Supplier(name=f"Test Supplier {uuid.uuid4()}", code=f"test-{uuid.uuid4().hex[:8]}")
    db_session.add(supplier)
    db_session.flush()

    # The upload still happens before parsing fails (see service.py) - the
    # original is preserved even for a run that ultimately fails, by
    # design, so track it for cleanup too via the supplier code/prefix.

    with pytest.raises(IngestionError):
        ingest_file(db_session, supplier_id=supplier.id, original_filename="empty.csv", content=b"")

    # ingest_file doesn't hand back the run on failure, so reconstruct the
    # key range to clean up: list and delete anything under this test
    # supplier's prefix.
    import boto3

    client = boto3.client("s3")
    prefix = f"sources/{supplier.code}/"
    response = client.list_objects_v2(Bucket=os.environ["S3_BUCKET_NAME"], Prefix=prefix)
    for obj in response.get("Contents", []):
        client.delete_object(Bucket=os.environ["S3_BUCKET_NAME"], Key=obj["Key"])
