"""
FastAPI application entrypoint.

This will become the OpenClaw-facing tool interface (build-order step 10)
plus whatever HTTP surface the owner-facing review/approval flow needs.
For now it only exposes a health check — routes get added as each module
(ingestion, matching, rules, ...) lands.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from adc_backend.config import get_settings
from adc_backend.modules.auth import require_api_key_dependency
from adc_backend.modules.tools.router import router as tools_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Deliberately logs only non-secret settings - never log a secret value
    # or a fetched credential, even at debug level.
    logger.info(
        "Starting adc-backend | environment=%s region=%s bucket=%s",
        settings.environment,
        settings.aws_region,
        settings.s3_bucket_name or "(unset)",
    )
    yield


app = FastAPI(
    title="Distribuidora Candelaria - Supplier List Automation",
    description="Internal backend. Prepares purchase decisions for owner approval; never executes them.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    # Deliberately unauthenticated - the ALB target group health check
    # hits this with no headers, and it exposes nothing sensitive.
    return {"status": "ok"}


# Every route below requires X-Api-Key - see modules/auth.py for why this
# exists at all and what it does (and doesn't) protect against.
app.include_router(tools_router, dependencies=[require_api_key_dependency])
