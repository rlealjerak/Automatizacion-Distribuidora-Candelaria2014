"""
MCP-facing tool surface for OpenClaw - the three tools
docs/openclaw/OPENCLAW_TECHNICAL_SPEC.md Section 4 specifies
(list_queue/approve_decision/reorder_queue), backed by
modules/approvals/service.py. Mounted under /mcp in main.py, wrapped in
modules/mcp_server/auth.py's bearer-token middleware.

Uses the official `mcp` Python SDK. Built against mcp 2.x, where the
high-level server class was renamed FastMCP -> MCPServer and
`stateless_http`/`json_response` moved from the constructor onto the
`streamable_http_app()` call - a real, version-specific break from what
older MCP docs/tutorials describe (verified by actually importing and
exercising this package locally before writing this file, not assumed
from possibly-stale documentation).

Each tool opens its own short-lived DB session (mirrors worker.py's
per-message session pattern) rather than going through FastAPI's
`Depends(get_db)`, since MCP tools aren't wired through FastAPI's
dependency-injection system at all - they're registered directly on the
MCPServer instance and reached via the mounted ASGI app.

`stateless_http=True` (deliberate, not in the spec): this backend's ECS
service can run more than one task behind the ALB, and stateless
streamable-http means no session/SSE affinity is needed across replicas
- any task can answer any request. `json_response=True` gives plain
request/response JSON instead of SSE streaming, appropriate for these
short, non-streaming tool calls.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.orm import Session
from starlette.applications import Starlette

from adc_backend.config import get_settings
from adc_backend.db.base import get_sessionmaker
from adc_backend.modules.approvals.models import ApprovalQueue
from adc_backend.modules.approvals.service import (
    ApprovalsServiceError,
    resolve_active_item,
)
from adc_backend.modules.approvals.service import (
    list_queue as list_queue_service,
)
from adc_backend.modules.approvals.service import (
    reorder_queue as reorder_queue_service,
)

logger = logging.getLogger(__name__)

mcp_server = MCPServer(name="candelaria-backend")


@contextlib.contextmanager
def _db_session() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _item_to_dict(item: ApprovalQueue, *, include_queue_position: bool) -> dict:
    payload = item.summary_payload or {}
    out = {
        "id": str(item.id),
        "type": item.type.value,
        "status": item.status.value,
        "summary": item.summary,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        **{k: payload[k] for k in ("asin", "description", "brand", "supplier_name", "classification") if k in payload},
    }
    if include_queue_position:
        out["queue_position"] = item.queue_position
    else:
        out["pending_started_at"] = item.pending_started_at.isoformat() if item.pending_started_at else None
        out["last_reminder_at"] = item.last_reminder_at.isoformat() if item.last_reminder_at else None
        out["reminder_count"] = item.reminder_count
    return out


@mcp_server.tool()
def list_queue() -> dict:
    """Returns the current active approval item (if any) plus all queued items, in order."""
    with _db_session() as db:
        active, queued = list_queue_service(db)
        return {
            "active_item": _item_to_dict(active, include_queue_position=False) if active else None,
            "queued_items": [_item_to_dict(i, include_queue_position=True) for i in queued],
        }


@mcp_server.tool()
def approve_decision(approval_id: str, resolution: Literal["approved", "denied"], note: str | None = None) -> dict:
    """
    Resolves the active approval item only. Rejects calls targeting a
    non-active item (approval_id) - the server-side backstop for the
    one-active-item model, never trusting the caller's claim about which
    item is active. Idempotent: a duplicate call with the same
    approval_id + resolution against an already-resolved item returns
    the same success result rather than erroring.
    """
    with _db_session() as db:
        result = resolve_active_item(db, approval_id, resolution, note)
        if not result.ok:
            return {"ok": False, "error": result.error, "active_approval_id": result.active_approval_id}
        return {
            "ok": True,
            "approval_id": result.approval_id,
            "resolution": result.resolution,
            "resolved_at": result.resolved_at.isoformat() if result.resolved_at else None,
            "next_active_item": (
                {"id": str(result.next_active_item.id), "summary": result.next_active_item.summary}
                if result.next_active_item
                else None
            ),
        }


@mcp_server.tool()
def reorder_queue(approval_id: str) -> dict:
    """
    Moves a queued item to the front (next up once the active item
    resolves). Only ever called on explicit client instruction - that
    constraint lives in OpenClaw's own agent instructions, not here.
    """
    with _db_session() as db:
        try:
            item = reorder_queue_service(db, approval_id)
        except ApprovalsServiceError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "approval_id": str(item.id), "new_queue_position": item.queue_position}


def build_mcp_asgi_app() -> Starlette:
    """
    Builds the mountable ASGI app. Mount at FastAPI root ("/"), not
    "/mcp" - streamable_http_app()'s own default internal route is
    already "/mcp" (confirmed by inspecting its route table locally), so
    mounting under an extra "/mcp" prefix in the parent app would double
    it up. Routes registered on the parent FastAPI app before this mount
    (e.g. /health, tools_router) still match first - Starlette only falls
    through to a root mount for paths nothing else claims.
    """
    settings = get_settings()
    allowed_hosts = [h.strip() for h in settings.mcp_allowed_hosts.split(",") if h.strip()]
    return mcp_server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts),
    )
