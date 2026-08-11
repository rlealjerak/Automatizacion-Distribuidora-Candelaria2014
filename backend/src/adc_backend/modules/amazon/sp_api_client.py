"""
Amazon Selling Partner API (SP-API) client.

*** PARTIALLY VERIFIED AGAINST LIVE SP-API (as of 2026-08-10). *** Real
LWA credentials are now stored in Secrets Manager, and `_get_access_token`
+ `search_catalog_items` have both been exercised against the real API -
a real LWA token exchange and a real catalog search returning real
Amazon products. `get_pricing`, `get_fees_estimate`, and
`get_listing_restrictions` have NOT been called against live data yet -
still only verified against mocked HTTP responses shaped like SP-API's
documented schema, same caveat as before for those three specifically.
Whoever calls them for the first time for real should treat that as the
actual verification, not assume it from the mocked tests passing.

Scope: catalog search (for text/brand matching, step 7), pricing/buy box,
fees estimate, and restrictions/gated status - the four SP-API capabilities
build-order step 5 calls for. Does not implement order/inventory/reports
APIs - out of scope for this system.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

import httpx

from adc_backend.config import get_secret, get_settings

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

# SP-API base endpoint by region. This system operates in the US
# marketplace only for MVP (Amazon.com, ATVPDKIKX0DER) - not configurable
# yet since nothing in CLAUDE.md's brief mentions multi-marketplace.
NA_BASE_URL = "https://sellingpartnerapi-na.amazon.com"
US_MARKETPLACE_ID = "ATVPDKIKX0DER"


class SPAPIError(Exception):
    pass


@dataclass
class CatalogMatch:
    asin: str
    title: str
    brand: str | None


@dataclass
class PricingSnapshot:
    asin: str
    current_price: Decimal | None
    buy_box_price: Decimal | None
    buy_box_owner: str | None  # seller id, or "AMAZON"
    amazon_has_buy_box: bool | None
    seller_count: int | None
    raw_response: dict


@dataclass
class FeesEstimate:
    asin: str
    referral_fee: Decimal | None
    fba_fee: Decimal | None
    other_fees: dict = field(default_factory=dict)
    raw_response: dict = field(default_factory=dict)


@dataclass
class RestrictionsResult:
    asin: str
    is_restricted: bool
    is_gated: bool
    approved_for_seller: bool  # only meaningful if is_gated
    raw_response: dict


class SPAPIClient:
    def __init__(self, http_client: httpx.Client | None = None):
        self._http = http_client or httpx.Client(timeout=30.0)
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    # --- auth ---

    def _credentials(self) -> dict:
        settings = get_settings()
        return get_secret(settings.sp_api_secret_name)

    def _get_access_token(self) -> str:
        """LWA bearer token, refreshed ~60s before actual expiry to avoid edge-of-window failures."""
        if self._access_token and time.monotonic() < self._token_expires_at - 60:
            return self._access_token

        creds = self._credentials()
        response = self._http.post(
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": creds["refresh_token"],
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
            },
        )
        if response.status_code != 200:
            raise SPAPIError(f"LWA token refresh failed: {response.status_code} {response.text}")
        body = response.json()
        self._access_token = body["access_token"]
        self._token_expires_at = time.monotonic() + body["expires_in"]
        return self._access_token

    def _request(self, method: str, path: str, **kwargs) -> dict:
        token = self._get_access_token()
        headers = {"x-amz-access-token": token, **kwargs.pop("headers", {})}
        response = self._http.request(method, f"{NA_BASE_URL}{path}", headers=headers, **kwargs)
        if response.status_code == 429:
            raise SPAPIError(f"SP-API rate limited on {path} - caller should back off and retry")
        if response.status_code >= 400:
            raise SPAPIError(f"SP-API {method} {path} failed: {response.status_code} {response.text}")
        return response.json()

    # --- catalog search (build-order step 5 / feeds step 7's matching engine) ---

    def search_catalog_items(
        self, keywords: str, marketplace_id: str = US_MARKETPLACE_ID, brand: str | None = None
    ) -> list[CatalogMatch]:
        params = {"keywords": keywords, "marketplaceIds": marketplace_id, "includedData": "summaries"}
        if brand:
            params["brandNames"] = brand
        body = self._request("GET", "/catalog/2022-04-01/items", params=params)

        matches: list[CatalogMatch] = []
        for item in body.get("items", []):
            summaries = item.get("summaries", [])
            summary = summaries[0] if summaries else {}
            matches.append(
                CatalogMatch(
                    asin=item["asin"],
                    title=summary.get("itemName", ""),
                    brand=summary.get("brand"),
                )
            )
        return matches

    # --- pricing / buy box ---

    def get_pricing(self, asin: str, marketplace_id: str = US_MARKETPLACE_ID) -> PricingSnapshot:
        body = self._request(
            "GET",
            f"/products/pricing/v0/items/{asin}/offers",
            params={"MarketplaceId": marketplace_id, "ItemCondition": "New"},
        )
        payload = body.get("payload", {})
        summary = payload.get("Summary", {})
        offers = payload.get("Offers", [])

        buy_box_offer = next((o for o in offers if o.get("IsBuyBoxWinner")), None)
        buy_box_price = None
        buy_box_owner = None
        amazon_has_buy_box = None
        if buy_box_offer:
            listing_price = buy_box_offer.get("ListingPrice", {})
            buy_box_price = _to_decimal(listing_price.get("Amount"))
            buy_box_owner = "AMAZON" if buy_box_offer.get("IsFulfilledByAmazon") and buy_box_offer.get(
                "SellerId"
            ) == "AMAZON" else buy_box_offer.get("SellerId")
            amazon_has_buy_box = buy_box_owner == "AMAZON"

        current_price = _to_decimal(summary.get("LowestPrices", [{}])[0].get("ListingPrice", {}).get("Amount")) if summary.get("LowestPrices") else None

        return PricingSnapshot(
            asin=asin,
            current_price=current_price,
            buy_box_price=buy_box_price,
            buy_box_owner=buy_box_owner,
            amazon_has_buy_box=amazon_has_buy_box,
            seller_count=summary.get("TotalOfferCount"),
            raw_response=body,
        )

    # --- fees ---

    def get_fees_estimate(
        self, asin: str, price: Decimal, marketplace_id: str = US_MARKETPLACE_ID
    ) -> FeesEstimate:
        body = self._request(
            "POST",
            f"/products/fees/v0/items/{asin}/feesEstimate",
            json={
                "FeesEstimateRequest": {
                    "MarketplaceId": marketplace_id,
                    "PriceToEstimateFees": {"ListingPrice": {"CurrencyCode": "USD", "Amount": float(price)}},
                    "Identifier": f"fee-estimate-{asin}",
                }
            },
        )
        result = body.get("payload", {}).get("FeesEstimateResult", {}).get("FeesEstimate", {})
        fee_details = result.get("FeeDetailList", [])

        referral_fee = None
        fba_fee = None
        other_fees: dict = {}
        for fee in fee_details:
            amount = _to_decimal(fee.get("FeeAmount", {}).get("Amount"))
            fee_type = fee.get("FeeType", "")
            if fee_type == "ReferralFee":
                referral_fee = amount
            elif fee_type in ("FBAFees", "FBAPerUnitFulfillmentFee"):
                fba_fee = amount
            else:
                other_fees[fee_type] = str(amount) if amount is not None else None

        return FeesEstimate(asin=asin, referral_fee=referral_fee, fba_fee=fba_fee, other_fees=other_fees, raw_response=body)

    # --- restrictions / gating ---

    def get_listing_restrictions(
        self, asin: str, seller_id: str, marketplace_id: str = US_MARKETPLACE_ID
    ) -> RestrictionsResult:
        body = self._request(
            "GET",
            "/listings/2021-08-01/restrictions",
            params={"asin": asin, "sellerId": seller_id, "marketplaceIds": marketplace_id},
        )
        restrictions = body.get("restrictions", [])
        # SP-API returns an empty list when there's no restriction at all.
        # A non-empty list means SOME reason exists - could be a hard
        # restriction (e.g. restricted category) or a gate the seller can
        # apply to clear. Reason codes distinguish the two; CLAUDE.md
        # treats them very differently (hard exclude vs. surface-if-strong),
        # so this must not collapse them into one flag.
        gated_reasons = {"APPROVAL_REQUIRED"}
        hard_restricted_reasons = {"ASIN_NOT_ELIGIBLE", "RESTRICTED_PRODUCT"}

        reason_codes = {
            reason.get("reasonCode")
            for restriction in restrictions
            for reason in restriction.get("reasons", [])
        }

        return RestrictionsResult(
            asin=asin,
            is_restricted=bool(reason_codes & hard_restricted_reasons),
            is_gated=bool(reason_codes & gated_reasons),
            approved_for_seller=len(restrictions) == 0,
            raw_response=body,
        )


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))
