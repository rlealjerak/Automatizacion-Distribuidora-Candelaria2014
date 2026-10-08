"""
Static-bearer-token auth for the /mcp surface - a separate credential
from modules/auth.py's X-Api-Key (different caller, different header,
different secret). Per docs/openclaw/BACKEND_CHANGES_FOR_OPENCLAW.md
Section 3: "new middleware validating a static bearer token
(CANDELARIA_BACKEND_TOKEN)".

This is deliberately a plain ASGI middleware wrapping only the mounted
MCP app, not one of `mcp`'s own OAuth/JWT auth providers (TokenVerifier,
JWTVerifier, etc.) - those exist for third-party-issued tokens verified
against a JWKS/introspection endpoint, which is the wrong shape for one
static shared secret we mint and store ourselves in Secrets Manager, the
same way api_key_secret_name already works for the X-Api-Key surface.
"""

from __future__ import annotations

import hmac
import json
from functools import lru_cache

from starlette.types import ASGIApp, Receive, Scope, Send

from adc_backend.config import get_secret, get_settings


@lru_cache
def _expected_token() -> str | None:
    settings = get_settings()
    if not settings.candelaria_backend_token_secret_name:
        return None  # local dev escape hatch - same pattern as modules/auth.py
    return get_secret(settings.candelaria_backend_token_secret_name)["token"]


class BearerAuthMiddleware:
    """Pure ASGI middleware - rejects with 401 before the wrapped MCP app ever sees the request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        expected = _expected_token()
        if expected is not None:
            headers = dict(scope.get("headers") or [])
            raw = headers.get(b"authorization", b"").decode("latin-1")
            provided = raw.removeprefix("Bearer ") if raw.startswith("Bearer ") else None
            if not provided or not hmac.compare_digest(provided, expected):
                body = json.dumps({"error": "Missing or invalid Authorization bearer token"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return

        await self.app(scope, receive, send)
