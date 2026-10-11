def test_health_returns_ok(app_client):
    # Uses the shared session-scoped app_client fixture (tests/conftest.py)
    # rather than its own `with TestClient(app)` - the app's lifespan can
    # only be entered once per process now that /mcp is mounted, see that
    # fixture's docstring.
    response = app_client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # "unknown" in local dev/test (no GIT_SHA build arg) - a real deploy
    # bakes in the actual commit, see Dockerfile.
    assert "commit" in body
