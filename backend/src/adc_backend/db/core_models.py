"""
Core entities shared across every module: Supplier and ListRun.

These don't belong to one downstream module (ingestion, matching, rules,
...) - they're referenced by all of them - so they live at the db/ level
rather than under modules/.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from adc_backend.db.base import Base


class SourceFileType(str, enum.Enum):
    EXCEL = "excel"
    CSV = "csv"
    PDF = "pdf"


class ListRunStatus(str, enum.Enum):
    UPLOADED = "uploaded"  # file stored to S3, not yet parsed
    MAPPING_PENDING = "mapping_pending"  # parsed; proposed column mapping awaiting owner confirmation
    MAPPING_CONFIRMED = "mapping_confirmed"  # owner confirmed mapping; queued for processing
    PROCESSING = "processing"  # background worker actively running matching/pricing/rules
    COMPLETED = "completed"  # all rows processed (individual rows may still be error/review)
    FAILED = "failed"  # run-level failure (not just per-row errors)
    PARTIAL = "partial"  # completed with a significant error/unprocessed-row count


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(nullable=False, unique=True)
    code: Mapped[str] = mapped_column(nullable=False, unique=True)  # short slug, e.g. "acme-wholesale"
    contact_name: Mapped[str | None]
    contact_email: Mapped[str | None]
    contact_phone: Mapped[str | None]
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    list_runs: Mapped[list[ListRun]] = relationship(back_populates="supplier")


class ListRun(Base):
    """One processing run of one uploaded supplier list."""

    __tablename__ = "list_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"), nullable=False)

    # Original file is immutable in S3 (see CLAUDE.md) - this run only ever
    # stores where it lives, never the file content itself.
    source_file_s3_key: Mapped[str] = mapped_column(nullable=False)
    source_file_original_filename: Mapped[str] = mapped_column(nullable=False)
    source_file_type: Mapped[SourceFileType] = mapped_column(Enum(SourceFileType, name="source_file_type"))

    status: Mapped[ListRunStatus] = mapped_column(
        Enum(ListRunStatus, name="list_run_status"), default=ListRunStatus.UPLOADED
    )

    # Confidence-scored proposed mapping (produced by normalization step) and
    # the owner-confirmed version - kept separate so a correction is visible,
    # not silently overwritten. See CLAUDE.md build-order step 4.
    proposed_column_mapping: Mapped[dict | None] = mapped_column(JSONB)
    confirmed_column_mapping: Mapped[dict | None] = mapped_column(JSONB)
    column_mapping_confirmed_at: Mapped[datetime | None]
    column_mapping_confirmed_by: Mapped[str | None]  # owner identity as given by OpenClaw; no user table yet

    total_rows: Mapped[int | None]
    processed_rows: Mapped[int] = mapped_column(default=0, server_default="0")
    error_rows: Mapped[int] = mapped_column(default=0, server_default="0")

    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    error_summary: Mapped[str | None] = mapped_column(Text)  # run-level failure reason, if status=failed

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    supplier: Mapped[Supplier] = relationship(back_populates="list_runs")
    raw_line_items: Mapped[list[RawLineItem]] = relationship(  # noqa: F821 - defined in modules.ingestion
        back_populates="list_run"
    )
