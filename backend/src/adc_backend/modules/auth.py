"""
Shared-API-key auth for the OpenClaw-facing HTTP surface.

This system has no other authentication anywhere (see the security review
that led to this module, 2026-08-15) - every route under tools/router.py
can upload files, trigger real SP-API/Keepa calls, confirm/reject ASIN
matches, and resolve review-queue entries. Before this backend sits
behind a public ALB, every one of those routes must require a valid
X-Api-Key header. This is deliberately simple (one shared key, not
per-caller credentials/OAuth/JWT) - OpenClaw is this system's only
intended caller per CLAUDE.md's architecture, not a multi-tenant public
API. Swap for something stronger later if that assumption ever changes.

The key is fetched from Secrets Manager once per process and cached -
unlike the RDS master password (db_secret_name), this key doesn't
auto-rotate, and checking it on every single request can't afford a
Secrets Manager round-trip each time. Redeploying picks up a new value if
it's ever rotated by hand.

Local dev escape hatch: if API_KEY_SECRET_NAME is unset, auth is skipped
entirely - same pattern as DATABASE_URL bypassing Secrets Manager for
local Postgres (see config.py). Must never be unset in a real environment.
"""

from __future__ import annotations

import hmac
from functools import lru_cache

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

from adc_backend.config import get_secret, get_settings

_api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)


@lru_cache
def _expected_api_key() -> str | None:
    settings = get_settings()
    if not settings.api_key_secret_name:
        return None  # local dev escape hatch - see module docstring
    return get_secret(settings.api_key_secret_name)["api_key"]


def require_api_key(provided: str | None = Security(_api_key_header)) -> None:
    expected = _expected_api_key()
    if expected is None:
        return  # local dev, no secret configured - see module docstring
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Api-Key header")


# FastAPI dependency for router-level enforcement (all routes in one
# router get this without repeating Depends(...) on every function).
require_api_key_dependency = Depends(require_api_key)
