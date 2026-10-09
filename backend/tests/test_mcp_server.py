"""
MCP server tests, split the same way test_auth.py is:
- BearerAuthMiddleware tested directly (pure logic, get_secret monkeypatched,
  no DB/AWS needed) - mirrors modules/auth.py's own test style.
- Full tool round-trips (list_queue/approve_decision/reorder_queue) against
  the real mounted /mcp app, real local Postgres - skipped without
  DATABASE_URL, same convention as test_approvals_service.py.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from adc_backend.config import get_settings
from adc_backend.modules.mcp_server import auth as mcp_auth
from adc_backend.modules.mcp_server.auth import BearerAuthMiddleware


@pytest.fixture(autouse=True)
def _reset_caches():
    get_settings.cache_clear()
    mcp_auth._expected_token.cache_clear()
    yield
    get_settings.cache_clear()
    mcp_auth._expected_token.cache_clear()


def _wrapped_app():
    async def ok(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    return Starlette(routes=[Route("/mcp", BearerAuthMiddleware(inner), methods=["POST"])])


def test_no_secret_configured_is_local_dev_bypass(monkeypatch):
    monkeypatch.delenv("CANDELARIA_BACKEND_TOKEN_SECRET_NAME", raising=False)
    client = TestClient(_wrapped_app())
    assert client.post("/mcp").status_code == 200


def test_missing_header_is_rejected_when_token_configured(monkeypatch):
    monkeypatch.setenv("CANDELARIA_BACKEND_TOKEN_SECRET_NAME", "adc/prod/openclaw-backend-token")
    monkeypatch.setattr(mcp_auth, "get_secret", lambda name: {"token": "real-token"})
    client = TestClient(_wrapped_app())
    r = client.post("/mcp")
    assert r.status_code == 401


def test_wrong_token_is_rejected(monkeypatch):
    monkeypatch.setenv("CANDELARIA_BACKEND_TOKEN_SECRET_NAME", "adc/prod/openclaw-backend-token")
    monkeypatch.setattr(mcp_auth, "get_secret", lambda name: {"token": "real-token"})
    client = TestClient(_wrapped_app())
    r = client.post("/mcp", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_correct_token_is_accepted(monkeypatch):
    monkeypatch.setenv("CANDELARIA_BACKEND_TOKEN_SECRET_NAME", "adc/prod/openclaw-backend-token")
    monkeypatch.setattr(mcp_auth, "get_secret", lambda name: {"token": "real-token"})
    client = TestClient(_wrapped_app())
    r = client.post("/mcp", headers={"Authorization": "Bearer real-token"})
    assert r.status_code == 200


pytestmark_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


@pytestmark_db
class TestMcpToolsRoundTrip:
    @pytest.fixture(autouse=True)
    def _no_auth(self, monkeypatch):
        monkeypatch.delenv("CANDELARIA_BACKEND_TOKEN_SECRET_NAME", raising=False)
        get_settings.cache_clear()
        mcp_auth._expected_token.cache_clear()

    @pytest.fixture(autouse=True)
    def _clean_approval_queue(self, db_session):
        # Unlike every other DB-backed test in this suite, these tests
        # commit (the MCP tool call runs in its own DB session over real
        # HTTP - see modules/mcp_server/server.py::_db_session - so a
        # rollback in db_session's own teardown can't reach it). Without
        # this, a committed row (in particular is_active_item=True) leaks
        # into every later test run against this same local Postgres,
        # silently breaking unrelated tests - found live the hard way
        # (test_approvals_service.py failures traced back to this file's
        # leftover data, not a bug in either file).
        from adc_backend.modules.approvals.models import ApprovalAuditLog, ApprovalQueue

        db_session.query(ApprovalAuditLog).delete()
        db_session.query(ApprovalQueue).delete()
        db_session.commit()
        yield
        db_session.query(ApprovalAuditLog).delete()
        db_session.query(ApprovalQueue).delete()
        db_session.commit()

    @pytest.fixture
    def client(self, app_client):
        # Delegates to the shared session-scoped app_client fixture
        # (tests/conftest.py) rather than entering `with TestClient(app)`
        # itself - MCPServer's StreamableHTTPSessionManager can only be
        # .run() once per process, so only one place in the whole test
        # suite may enter the app's lifespan. See that fixture's
        # docstring for the full story (found live, not assumed).
        return app_client

    @pytest.fixture
    def db_session(self):
        from adc_backend.db import models  # noqa: F401
        from adc_backend.db.base import get_sessionmaker

        session = get_sessionmaker()()
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    def _seed_buy_item(self, db):
        from adc_backend.db.core_models import ListRun, SourceFileType, Supplier
        from adc_backend.modules.amazon.models import AmazonDataSnapshot
        from adc_backend.modules.approvals.service import create_from_classification
        from adc_backend.modules.ingestion.models import RawLineItem
        from adc_backend.modules.rules.models import ClassificationLabel, ClassificationResult

        supplier = Supplier(name=f"MCP Test Supplier {uuid.uuid4()}", code=f"mcp-test-{uuid.uuid4().hex[:8]}")
        db.add(supplier)
        db.flush()
        run = ListRun(
            supplier_id=supplier.id, source_file_s3_key="s3://x/y.csv",
            source_file_original_filename="y.csv", source_file_type=SourceFileType.CSV,
        )
        db.add(run)
        db.flush()
        item = RawLineItem(list_run_id=run.id, row_number=1, raw_data={}, unit_price=Decimal("10.00"), description="MCP Widget")
        db.add(item)
        db.flush()
        snapshot = AmazonDataSnapshot(list_run_id=run.id, raw_line_item_id=item.id, asin="B0MCPTEST1", current_price=Decimal("20.00"))
        db.add(snapshot)
        db.flush()
        trace = [{"rule": "financial_calculation", "result": "info", "reasoning": "x", "inputs": {"unit_cost": "10.00", "sell_price": "20.00", "profit": "5.00"}}]
        result = ClassificationResult(
            list_run_id=run.id, raw_line_item_id=item.id, amazon_data_snapshot_id=snapshot.id,
            classification=ClassificationLabel.BUY, roi=Decimal("50.0"), margin=Decimal("25.0"), rule_trace=trace,
        )
        db.add(result)
        db.flush()
        item_row = create_from_classification(db, result.id)
        db.commit()
        return item_row

    @staticmethod
    def _call(client, name, arguments):
        """
        Returns the tool's actual result dict, unwrapped from the two
        layers of JSON around it: the outer JSON-RPC envelope, and the
        `content[0].text` field, which is itself a JSON-encoded string
        (the SDK's default text-content representation of a tool's dict
        return value) - a naive substring check against the raw response
        text sees escaped quotes (\\"ok\\":true) and never matches.
        """
        import json

        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        envelope = response.json()
        result_text = envelope["result"]["content"][0]["text"]
        return response, json.loads(result_text)

    def test_list_queue_reflects_seeded_active_item(self, client, db_session):
        item = self._seed_buy_item(db_session)
        response, result = self._call(client, "list_queue", {})
        assert response.status_code == 200
        assert result["active_item"]["id"] == str(item.id)
        assert result["active_item"]["description"] == "MCP Widget"

    def test_approve_decision_on_active_item_succeeds(self, client, db_session):
        item = self._seed_buy_item(db_session)
        response, result = self._call(client, "approve_decision", {"approval_id": str(item.id), "resolution": "approved"})
        assert response.status_code == 200
        assert result["ok"] is True
        assert result["approval_id"] == str(item.id)

    def test_approve_decision_on_wrong_id_returns_error_shape(self, client, db_session):
        self._seed_buy_item(db_session)
        response, result = self._call(client, "approve_decision", {"approval_id": str(uuid.uuid4()), "resolution": "approved"})
        assert response.status_code == 200
        assert result["ok"] is False
        assert "active_approval_id" in result

    def test_revoke_decision_on_approved_item_succeeds(self, client, db_session):
        item = self._seed_buy_item(db_session)
        self._call(client, "approve_decision", {"approval_id": str(item.id), "resolution": "approved"})

        response, result = self._call(client, "revoke_decision", {"approval_id": str(item.id), "reason": "client changed their mind"})
        assert response.status_code == 200
        assert result["ok"] is True
        assert result["approval_id"] == str(item.id)
        assert result["new_status"] == "pending"  # queue was empty, so it becomes active again

    def test_revoke_decision_on_pending_item_returns_error_shape(self, client, db_session):
        item = self._seed_buy_item(db_session)  # never approved - still pending
        response, result = self._call(client, "revoke_decision", {"approval_id": str(item.id), "reason": "oops"})
        assert response.status_code == 200
        assert result["ok"] is False
        assert "error" in result
