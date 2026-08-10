"""
S3 storage for original uploaded supplier files.

Per CLAUDE.md: original files are immutable and versioned, never modified
in place. This module only ever PUTs a new object under a run-specific key
(one run = one immutable object) and GETs bytes back for parsing - there is
no update/overwrite path here by design. Bucket versioning (enabled in
infra/modules/s3) is the safety net if that rule is ever violated by
accident (e.g. a retry reusing a key).
"""

from __future__ import annotations

import uuid
from functools import lru_cache

import boto3

from adc_backend.config import get_settings

SOURCES_PREFIX = "sources"  # original uploaded files, one object per run
EXPORTS_PREFIX = "exports"  # generated exports (step 11), separate lifecycle from sources


@lru_cache
def _s3_client():
    return boto3.client("s3", region_name=get_settings().aws_region)


def build_source_key(supplier_code: str, run_id: uuid.UUID, original_filename: str) -> str:
    """
    One object per run - never reused, never overwritten. Supplier code in
    the key path keeps a bucket listing human-browsable without a DB
    lookup; run_id guarantees uniqueness even if the same filename is
    re-uploaded.
    """
    return f"{SOURCES_PREFIX}/{supplier_code}/{run_id}/{original_filename}"


def upload_source_file(key: str, content: bytes, content_type: str | None = None) -> None:
    extra = {"ContentType": content_type} if content_type else {}
    _s3_client().put_object(
        Bucket=get_settings().s3_bucket_name,
        Key=key,
        Body=content,
        **extra,
    )


def download_source_file(key: str) -> bytes:
    response = _s3_client().get_object(Bucket=get_settings().s3_bucket_name, Key=key)
    return response["Body"].read()
