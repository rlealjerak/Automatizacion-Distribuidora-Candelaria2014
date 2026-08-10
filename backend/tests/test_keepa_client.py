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

    client = _client_with_transport(handler)
    with pytest.raises(KeepaError):
        client.get_product("B000000001")
