"""
Shared pytest fixtures.

`app_client`: the FastAPI app's lifespan can now only be entered once per
process - since /mcp was mounted (modules/mcp_server/), MCPServer's
StreamableHTTPSessionManager raises "can only be called once per
instance" on a second `with TestClient(app)` cycle in the same process
(found live while wiring this in, not documented anywhere obvious as a
constraint of the underlying `mcp` SDK). Every test that needs the real
app running end-to-end (not a route function called directly, and not a
narrower ASGI app built just for one test) must share this one
session-scoped fixture rather than each independently doing
`with TestClient(app) as client:` the way test_health.py alone used to
before this constraint existed.

base_url="http://localhost" (not TestClient's "testserver" default):
settings.mcp_allowed_hosts defaults to "127.0.0.1,localhost" (see
config.py) - matching the Host header to that avoids the /mcp transport's
DNS-rebinding host check (a real 421 otherwise), and is harmless for
every other route, which doesn't look at the Host header at all.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def app_client():
    from adc_backend.main import app

    with TestClient(app, base_url="http://localhost") as client:
        yield client
