"""
Auth dependency tests - pure logic, no DB/AWS needed (get_secret is
monkeypatched). Covers the two things that matter most: a real key is
enforced when configured, and the local-dev bypass only kicks in when
truly unconfigured (never silently skips auth for a wrong/missing key).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from adc_backend.config import get_settings
from adc_backend.modules import auth


@pytest.fixture(autouse=True)
def _reset_caches():
    """Every test gets a clean get_settings()/_expected_api_key() cache -
    both are @lru_cache'd for production performance, which would
    otherwise leak state between tests that set different env vars."""
    get_settings.cache_clear()
    auth._expected_api_key.cache_clear()
    yield
    get_settings.cache_clear()
    auth._expected_api_key.cache_clear()


def test_no_secret_configured_is_local_dev_bypass(monkeypatch):
    monkeypatch.delenv("API_KEY_SECRET_NAME", raising=False)
    auth.require_api_key(provided=None)  # does not raise


def test_missing_header_is_rejected_when_key_configured(monkeypatch):
    monkeypatch.setenv("API_KEY_SECRET_NAME", "adc/prod/api-key")
    monkeypatch.setattr(auth, "get_secret", lambda name: {"api_key": "real-secret-key"})
    with pytest.raises(HTTPException) as exc_info:
        auth.require_api_key(provided=None)
    assert exc_info.value.status_code == 401


def test_wrong_key_is_rejected(monkeypatch):
    monkeypatch.setenv("API_KEY_SECRET_NAME", "adc/prod/api-key")
    monkeypatch.setattr(auth, "get_secret", lambda name: {"api_key": "real-secret-key"})
    with pytest.raises(HTTPException) as exc_info:
        auth.require_api_key(provided="wrong-key")
    assert exc_info.value.status_code == 401


def test_correct_key_is_accepted(monkeypatch):
    monkeypatch.setenv("API_KEY_SECRET_NAME", "adc/prod/api-key")
    monkeypatch.setattr(auth, "get_secret", lambda name: {"api_key": "real-secret-key"})
    auth.require_api_key(provided="real-secret-key")  # does not raise


def test_health_endpoint_needs_no_key(monkeypatch):
    """/health is deliberately outside the authenticated router - the ALB
    target group health check has no way to send a header."""
    monkeypatch.delenv("API_KEY_SECRET_NAME", raising=False)
    from fastapi.testclient import TestClient

    from adc_backend.main import app

    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
