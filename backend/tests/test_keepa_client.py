"""Keepa client tests against mocked HTTP responses shaped like Keepa's documented schema - see keepa_client.py's caveat."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from adc_backend.modules.amazon.keepa_client import KeepaClient, KeepaError


@pytest.fixture(autouse=True)
def _no_real_secrets_manager(monkeypatch):
    monkeypatch.setattr("adc_backend.modules.amazon.keepa_client.get_secret", lambda name: {"api_key": "fake-key"})


def _client_with_transport(handler) -> KeepaClient:
    return KeepaClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_get_product_parses_stats():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["asin"] == "B000000001"
        assert request.url.params["key"] == "fake-key"
        return httpx.Response(
            200,
            json={
                "products": [
                    {
                        "stats": {
                            "current": [1999, -1, -1, 4500],  # [price_cents, ?, ?, sales_rank]
                            "avg30": [2099, -1, -1, 5200],
                            "avg90": [2199, -1, -1, 6000],
                            "salesRankDrops30": 62,
                            "salesRankDrops90": 180,
                        },
                        "outOfStockPercentage90": {"Amazon": 3},
                    }
                ]
            },
        )

    client = _client_with_transport(handler)
    snapshot = client.get_product("B000000001")

    assert snapshot.current_sales_rank == 4500
    assert snapshot.avg_sales_rank_30d == 5200
    assert snapshot.sales_rank_drops_30d == 62
    assert snapshot.sales_rank_drops_90d == 180
    assert snapshot.current_price == Decimal("19.99")
    assert snapshot.avg_price_90d == Decimal("21.99")
    assert snapshot.out_of_stock_percentage_90d == 3


def test_keepa_no_data_sentinel_becomes_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"products": [{"stats": {"current": [-1, -1, -1, -1], "avg90": [-1]}}]},
        )

    client = _client_with_transport(handler)
    snapshot = client.get_product("B000000001")
    assert snapshot.current_sales_rank is None
    assert snapshot.current_price is None
    assert snapshot.avg_price_90d is None


def test_no_products_raises_keepa_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"products": []})

    client = _client_with_transport(handler)
    with pytest.raises(KeepaError):
        client.get_product("B0INVALID")


def test_http_error_raises_keepa_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too Many Requests")

    # sleep_fn injected: a 429 is now retried after waiting for a token
    # refill (2026-11-14) - without this the test sleeps for real (~5 min).
    client = KeepaClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)), sleep_fn=lambda s: None)
    with pytest.raises(KeepaError):
        client.get_product("B000000001")


def test_non_429_http_error_raises_immediately_without_waiting():
    slept: list[float] = []
    client = KeepaClient(
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "bad asin"}))),
        sleep_fn=slept.append,
    )
    with pytest.raises(KeepaError):
        client.get_product("B000000001")
    assert slept == []


# --- token pacing (added 2026-11-14: the real plan refills 1 token/minute) ---

_OK_PRODUCT = {"products": [{"stats": {"current": [1999, -1, -1, 4500], "salesRankDrops30": 62}}]}


def test_waits_for_refill_on_429_then_succeeds():
    responses = iter([
        httpx.Response(429, json={"tokensLeft": -3, "refillIn": 42_000}),
        httpx.Response(200, json={**_OK_PRODUCT, "tokensLeft": 0, "refillIn": 60_000}),
    ])
    slept: list[float] = []
    client = KeepaClient(http_client=httpx.Client(transport=httpx.MockTransport(lambda r: next(responses))), sleep_fn=slept.append)

    snapshot = client.get_product("B000000001")
    assert snapshot.sales_rank_drops_30d == 62
    assert slept == [42.0]  # Keepa's own refillIn, not a guessed delay
    assert client.token_wait_seconds == 42.0


def test_waits_preemptively_when_last_response_said_bucket_is_empty():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={**_OK_PRODUCT, "tokensLeft": 0, "refillIn": 30_000})

    slept: list[float] = []
    client = KeepaClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)), sleep_fn=slept.append)
    client.get_product("B000000001")
    assert slept == []
    client.get_product("B000000002")
    assert slept == [30.0]  # waited before spending a request on a guaranteed 429
    assert calls["n"] == 2
    assert client.lookup_count == 2


def test_gives_up_after_bounded_waits():
    slept: list[float] = []
    client = KeepaClient(
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, json={"tokensLeft": -1, "refillIn": 500_000}))),
        sleep_fn=slept.append,
    )
    with pytest.raises(KeepaError, match="out of tokens"):
        client.get_product("B000000001")
    assert len(slept) == 5
    assert all(s == 120.0 for s in slept)  # a single wait is capped, never an unbounded sleep


def test_api_key_fetched_once_per_client(monkeypatch):
    fetches = {"n": 0}

    def fake_get_secret(name):
        fetches["n"] += 1
        return {"api_key": "fake-key"}

    monkeypatch.setattr("adc_backend.modules.amazon.keepa_client.get_secret", fake_get_secret)
    client = _client_with_transport(lambda r: httpx.Response(200, json=_OK_PRODUCT))
    client.get_product("B000000001")
    client.get_product("B000000002")
    assert fetches["n"] == 1


def test_error_message_does_not_echo_response_body():
    client = _client_with_transport(lambda r: httpx.Response(500, text="internal detail key=fake-key"))
    with pytest.raises(KeepaError) as exc:
        client.get_product("B000000001")
    assert "fake-key" not in str(exc.value)
