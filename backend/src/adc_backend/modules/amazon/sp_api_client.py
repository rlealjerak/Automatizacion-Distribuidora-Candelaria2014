"""
Amazon Selling Partner API (SP-API) client.

*** FULLY LIVE-VERIFIED (as of 2026-08-15). *** All four capabilities -
search_catalog_items (2026-08-10), get_pricing, get_fees_estimate, and
get_listing_restrictions (2026-08-15) - have now been called against the
real API with real credentials and returned real data. That final round
of live testing found and fixed two real, load-bearing bugs the mocked
tests couldn't catch because the mocks were shaped by the same
assumptions that turned out wrong:

1. get_fees_estimate never sent IsAmazonFulfilled=True - SP-API silently
   estimated merchant-fulfilled fees instead of FBA fees, so fba_fee came
   back None on every real ASIN tested despite this being an FBA-only
   business. Fixed by adding that field to the request.
2. get_listing_restrictions didn't recognize NOT_ELIGIBLE at all (the
   code guessed at ASIN_NOT_ELIGIBLE/RESTRICTED_PRODUCT, neither of which
   SP-API actually returned) and didn't scope the request to a condition
   type - so a genuinely restricted real ASIN came back as neither
   restricted nor gated. Fixed by scoping to conditionType=new_new (this
   system only ever sources new product). NOT_ELIGIBLE turned out to be
   more nuanced than a simple hard-restriction code, though: the SAME
   code appeared on the same ASIN meaning both "permanently restricted"
   and "you can become an authorized seller by contacting the brand" -
   Amazon doesn't distinguish these at the reason-code level. Rather than
   guess from the message text (not a stable contract), NOT_ELIGIBLE gets
   its own ambiguous_restriction flag and routes to manual review - a
   user decision made explicitly after seeing this live evidence, not a
   silent engineering call. See RestrictionsResult and rules/engine.py.

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
    # True when SP-API returned NOT_ELIGIBLE - a reason code confirmed live
    # (2026-08-15) to cover BOTH a permanent hard restriction and a
    # clearable brand-authorization gate, with no reliable way to tell
    # them apart from the reason code alone (the human-readable message
    # text differs, but that's not a stable API contract to parse against).
    # Per CLAUDE.md's "any genuinely ambiguous case -> manual review, never
    # guess": this does NOT set is_restricted, so it isn't hard-excluded,
    # but the rule engine must route it to manual review rather than
    # treating it as a clean, sellable product either.
    ambiguous_restriction: bool
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
                    # This system exists for an FBA wholesale business (see CLAUDE.md) -
                    # every product it evaluates is assumed FBA. Without this, SP-API
                    # silently defaults to merchant-fulfilled and never returns an FBA
                    # fee line item at all (found live: fba_fee came back None on every
                    # real ASIN tested until this was added - not absent data, a wrong
                    # request).
                    "IsAmazonFulfilled": True,
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
            # conditionType scopes the response to one condition. Without it,
            # SP-API returns a separate restriction entry PER condition type
            # (new_new, used_good, collectible_like_new, ...) and this
            # system - which only ever sources new wholesale product, same
            # assumption get_pricing makes with ItemCondition=New - must not
            # aggregate reason codes across conditions it doesn't sell in
            # (found live: a real ASIN was restricted in every condition,
            # but scoping still matters in general - a used-only restriction
            # must not exclude an otherwise-sellable-as-new item).
            params={
                "asin": asin,
                "sellerId": seller_id,
                "marketplaceIds": marketplace_id,
                "conditionType": "new_new",
            },
        )
        restrictions = body.get("restrictions", [])
        # SP-API returns an empty list when there's no restriction at all.
        # A non-empty list means SOME reason exists - could be a hard
        # restriction (e.g. restricted category), a gate the seller can
        # apply to clear, or (see ambiguous_reasons below) both at once
        # depending on category. Reason codes are meant to distinguish
        # these; CLAUDE.md treats them very differently (hard exclude vs.
        # surface-if-strong vs. needs-a-human), so this must not collapse
        # them into one flag.
        #
        # Confirmed live 2026-08-15 against a real, currently-restricted
        # ASIN. ASIN_NOT_ELIGIBLE/RESTRICTED_PRODUCT were the original
        # guessed hard-restriction codes and have still never been seen in
        # a real response - kept in case another category uses them.
        # APPROVAL_REQUIRED (clean gate) also hasn't been seen live yet.
        # NOT_ELIGIBLE is what actually came back, and turned out to cover
        # two different real situations under the exact same code: this
        # ASIN's collectible conditions all said "we are currently not
        # accepting applications" (permanent), while its new_new condition
        # said "you are not approved... contact the brand owner to become
        # an authorized seller" (a clearable brand gate) - same reasonCode
        # both times. Since the code alone can't disambiguate and the
        # message text isn't a stable contract to parse, NOT_ELIGIBLE
        # deliberately does NOT set is_restricted - it's routed to manual
        # review instead (see ambiguous_restriction below).
        gated_reasons = {"APPROVAL_REQUIRED"}
        hard_restricted_reasons = {"ASIN_NOT_ELIGIBLE", "RESTRICTED_PRODUCT"}
        ambiguous_reasons = {"NOT_ELIGIBLE"}

        reason_codes = {
            reason.get("reasonCode")
            for restriction in restrictions
            for reason in restriction.get("reasons", [])
        }

        return RestrictionsResult(
            asin=asin,
            is_restricted=bool(reason_codes & hard_restricted_reasons),
            is_gated=bool(reason_codes & gated_reasons),
            ambiguous_restriction=bool(reason_codes & ambiguous_reasons),
            approved_for_seller=len(restrictions) == 0,
            raw_response=body,
        )


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))
