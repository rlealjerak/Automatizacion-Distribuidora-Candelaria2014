"""
Keepa API client - sales rank history/trend and price history.

*** LIVE-VERIFIED AGAINST REAL KEEPA (as of 2026-08-15). *** A real API
key is now in Secrets Manager; get_product() has been called against
several real ASINs and returned real titles/prices/sales ranks (e.g.
"0439023483" -> "The Hunger Games", rank 2577, price $11.94,
salesRankDrops30=46). Unit tests here still run against mocked responses
shaped like Keepa's documented schema, for speed/determinism - the live
call was the one-time verification that the contract assumed there
matches reality.

That live check is also what caught a real bug: Keepa's -1 "no data"
sentinel was normalized for the current/avg30/avg90 array fields but not
for the scalar salesRankDrops30/90 fields, so an untracked ASIN's -1 was
flowing through as a literal (very-low-velocity) number instead of None
(no data - needs manual review). Fixed below (_none_if_negative).

Honest scope note: Keepa does not report "estimated units sold per
month" as a direct field - that figure is something third-party tools
derive with their own modeling, which this system doesn't have and
shouldn't fabricate. What Keepa actually provides, and what this client
surfaces, is `salesRankDrops30`/`90`/`180` - a count of sales-rank-improved
events over each window, which Keepa itself documents as the standard
velocity proxy. CLAUDE.md's "≥50 units/month, with seasonality awareness,
using 30/60/90-day data" rule (build-order step 8) is implemented against
this proxy, not a fabricated unit-sales number - flagged here so nobody
mistakes sales_rank_drops_30d for an actual sold-units count later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import httpx

from adc_backend.config import get_secret, get_settings

KEEPA_BASE_URL = "https://api.keepa.com"
AMAZON_COM_DOMAIN_ID = 1  # Keepa's numeric code for the US marketplace - the only one this system uses

# Keepa timestamps are minutes since 2011-01-01 00:00 UTC ("Keepa time");
# not decoded here since nothing downstream needs absolute history
# timestamps yet, only the aggregate stats Keepa computes server-side via
# the `stats` request parameter.


class KeepaError(Exception):
    pass


@dataclass
class KeepaSnapshot:
    asin: str
    current_sales_rank: int | None
    avg_sales_rank_30d: int | None
    sales_rank_drops_30d: int | None  # velocity proxy - see module docstring
    sales_rank_drops_90d: int | None
    current_price: Decimal | None
    avg_price_90d: Decimal | None
    out_of_stock_percentage_90d: int | None
    raw_response: dict = field(default_factory=dict)


class KeepaClient:
    def __init__(self, http_client: httpx.Client | None = None):
        self._http = http_client or httpx.Client(timeout=30.0)

    def _api_key(self) -> str:
        settings = get_settings()
        return get_secret(settings.keepa_secret_name)["api_key"]

    def get_product(self, asin: str, stats_days: int = 90) -> KeepaSnapshot:
        response = self._http.get(
            f"{KEEPA_BASE_URL}/product",
            params={
                "key": self._api_key(),
                "domain": AMAZON_COM_DOMAIN_ID,
                "asin": asin,
                "stats": stats_days,
            },
        )
        if response.status_code >= 400:
            raise KeepaError(f"Keepa product lookup failed for {asin}: {response.status_code} {response.text}")

        body = response.json()
        products = body.get("products") or []
        if not products:
            raise KeepaError(f"Keepa returned no product data for {asin} - invalid ASIN or not tracked yet")
        product = products[0]
        stats = product.get("stats") or {}

        return KeepaSnapshot(
            asin=asin,
            current_sales_rank=_none_if_missing(stats.get("current", [None] * 4), 3),
            avg_sales_rank_30d=_none_if_missing(stats.get("avg30", [None] * 4), 3),
            sales_rank_drops_30d=_none_if_negative(stats.get("salesRankDrops30")),
            sales_rank_drops_90d=_none_if_negative(stats.get("salesRankDrops90")),
            current_price=_cents_to_decimal(_none_if_missing(stats.get("current", [None]), 0)),
            avg_price_90d=_cents_to_decimal(_none_if_missing(stats.get("avg90", [None]), 0)),
            out_of_stock_percentage_90d=(product.get("outOfStockPercentage90") or {}).get("Amazon"),
            raw_response=body,
        )


def _none_if_missing(values: list, index: int) -> int | None:
    """Keepa uses -1 as its own 'no data' sentinel throughout the CSV/stats arrays."""
    if index >= len(values):
        return None
    return _none_if_negative(values[index])


def _none_if_negative(value: int | None) -> int | None:
    """Same -1 'no data' sentinel, for scalar (non-array) stats fields like salesRankDrops30/90."""
    if value is None or value == -1:
        return None
    return value


def _cents_to_decimal(cents: int | None) -> Decimal | None:
    if cents is None:
        return None
    return Decimal(cents) / 100
