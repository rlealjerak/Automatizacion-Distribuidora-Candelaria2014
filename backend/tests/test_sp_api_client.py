"""
SP-API client tests against mocked HTTP responses shaped like SP-API's
documented schema - NOT recorded real responses (no real credentials
exist - see sp_api_client.py's module docstring). This verifies the
client's parsing/plumbing logic is internally consistent with the
documented contract; it is not proof the contract assumptions are
correct against the real API. Uses monkeypatched Secrets Manager access
so no AWS calls happen either.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from adc_backend.modules.amazon.sp_api_client import SPAPIClient, SPAPIError

FAKE_CREDS = {
    "refresh_token": "Atzr|fake",
    "client_id": "amzn1.application-oa2-client.fake",
    "client_secret": "fake-secret",
}


@pytest.fixture(autouse=True)
def _no_real_secrets_manager(monkeypatch):
    monkeypatch.setattr("adc_backend.modules.amazon.sp_api_client.get_secret", lambda name: FAKE_CREDS)


def _client_with_transport(handler) -> SPAPIClient:
    transport = httpx.MockTransport(handler)
    return SPAPIClient(http_client=httpx.Client(transport=transport))


def _lwa_response(request: httpx.Request) -> httpx.Response:
    assert request.url == "https://api.amazon.com/auth/o2/token"
    return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})


def test_get_access_token_calls_lwa_and_caches():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _lwa_response(request)

    client = _client_with_transport(handler)
    token1 = client._get_access_token()
    token2 = client._get_access_token()
    assert token1 == token2 == "fake-token"
    assert calls["count"] == 1  # second call used the cached token, not a new LWA request


def test_lwa_failure_raises_sp_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    client = _client_with_transport(handler)
    with pytest.raises(SPAPIError):
        client._get_access_token()


def test_search_catalog_items_parses_summaries():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/o2/token" or "amazon.com/auth" in str(request.url):
            return _lwa_response(request)
        assert request.url.path == "/catalog/2022-04-01/items"
        assert request.url.params["keywords"] == "blue widget"
        return httpx.Response(
            200,
            json={
                "items": [
                    {"asin": "B000000001", "summaries": [{"itemName": "Blue Widget 10-pack", "brand": "Acme"}]},
                    {"asin": "B000000002", "summaries": []},
                ]
            },
        )

    client = _client_with_transport(handler)
    matches = client.search_catalog_items("blue widget")
    assert len(matches) == 2
    assert matches[0].asin == "B000000001"
    assert matches[0].title == "Blue Widget 10-pack"
    assert matches[0].brand == "Acme"
    assert matches[1].title == ""  # missing summaries handled without crashing


def test_get_pricing_identifies_buy_box_winner():
    def handler(request: httpx.Request) -> httpx.Response:
        if "amazon.com/auth" in str(request.url):
            return _lwa_response(request)
        return httpx.Response(
            200,
            json={
                "payload": {
                    "Summary": {
                        "TotalOfferCount": 5,
                        "LowestPrices": [{"ListingPrice": {"Amount": 9.99}}],
                    },
                    "Offers": [
                        {"SellerId": "A1B2C3", "IsBuyBoxWinner": False, "ListingPrice": {"Amount": 10.99}},
                        {
                            "SellerId": "AMAZON",
                            "IsBuyBoxWinner": True,
                            "IsFulfilledByAmazon": True,
                            "ListingPrice": {"Amount": 9.99},
                        },
                    ],
                }
            },
        )

    client = _client_with_transport(handler)
    pricing = client.get_pricing("B000000001")
    assert pricing.buy_box_price == Decimal("9.99")
    assert pricing.amazon_has_buy_box is True
    assert pricing.seller_count == 5
    assert pricing.current_price == Decimal("9.99")


def test_get_pricing_no_buy_box_winner_leaves_fields_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if "amazon.com/auth" in str(request.url):
            return _lwa_response(request)
        return httpx.Response(200, json={"payload": {"Summary": {"TotalOfferCount": 2}, "Offers": []}})

    client = _client_with_transport(handler)
    pricing = client.get_pricing("B000000001")
    assert pricing.buy_box_price is None
    assert pricing.amazon_has_buy_box is None
    assert pricing.seller_count == 2


def test_get_fees_estimate_separates_referral_and_fba():
    def handler(request: httpx.Request) -> httpx.Response:
        if "amazon.com/auth" in str(request.url):
            return _lwa_response(request)
        # Regression test: without IsAmazonFulfilled=True, SP-API silently
        # estimates merchant-fulfilled fees and never returns an FBA fee
        # line item at all (found live 2026-08-15 - fba_fee came back None
        # on every real ASIN tested until this was set).
        import json as _json

        request_body = _json.loads(request.content)
        assert request_body["FeesEstimateRequest"]["IsAmazonFulfilled"] is True
        return httpx.Response(
            200,
            json={
                "payload": {
                    "FeesEstimateResult": {
                        "FeesEstimate": {
                            "FeeDetailList": [
                                {"FeeType": "ReferralFee", "FeeAmount": {"Amount": 1.50}},
                                {"FeeType": "FBAFees", "FeeAmount": {"Amount": 3.25}},
                                {"FeeType": "VariableClosingFee", "FeeAmount": {"Amount": 0.10}},
                            ]
                        }
                    }
                }
            },
        )

    client = _client_with_transport(handler)
    fees = client.get_fees_estimate("B000000001", Decimal("19.99"))
    assert fees.referral_fee == Decimal("1.50")
    assert fees.fba_fee == Decimal("3.25")
    assert fees.other_fees == {"VariableClosingFee": "0.1"}  # Decimal(str(0.10)) - float repr drops the trailing zero


def test_get_listing_restrictions_distinguishes_gated_from_hard_restricted():
    def handler(request: httpx.Request) -> httpx.Response:
        if "amazon.com/auth" in str(request.url):
            return _lwa_response(request)
        return httpx.Response(
            200,
            json={"restrictions": [{"marketplaceId": "ATVPDKIKX0DER", "reasons": [{"reasonCode": "APPROVAL_REQUIRED"}]}]},
        )

    client = _client_with_transport(handler)
    result = client.get_listing_restrictions("B000000001", seller_id="A1SELLER")
    assert result.is_gated is True
    assert result.is_restricted is False
    assert result.approved_for_seller is False


def test_get_listing_restrictions_not_eligible_is_ambiguous_not_hard_restricted():
    """NOT_ELIGIBLE is the reason code SP-API actually returns in practice
    (found live 2026-08-15) - and the SAME code covers both a permanent
    restriction and a clearable brand-authorization gate on the real ASIN
    it was found on. Per an explicit user decision, this must route to
    manual review (ambiguous_restriction), not silently hard-exclude a
    possibly-obtainable product nor silently show it as clean."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "amazon.com/auth" in str(request.url):
            return _lwa_response(request)
        assert request.url.params["conditionType"] == "new_new"
        return httpx.Response(
            200,
            json={"restrictions": [{"marketplaceId": "ATVPDKIKX0DER", "reasons": [{"reasonCode": "NOT_ELIGIBLE"}]}]},
        )

    client = _client_with_transport(handler)
    result = client.get_listing_restrictions("B000000001", seller_id="A1SELLER")
    assert result.is_restricted is False
    assert result.is_gated is False
    assert result.ambiguous_restriction is True
    assert result.approved_for_seller is False


def test_get_listing_restrictions_no_restrictions_means_approved():
    def handler(request: httpx.Request) -> httpx.Response:
        if "amazon.com/auth" in str(request.url):
            return _lwa_response(request)
        return httpx.Response(200, json={"restrictions": []})

    client = _client_with_transport(handler)
    result = client.get_listing_restrictions("B000000001", seller_id="A1SELLER")
    assert result.is_gated is False
    assert result.is_restricted is False
    assert result.approved_for_seller is True


def test_rate_limit_raises_sp_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if "amazon.com/auth" in str(request.url):
            return _lwa_response(request)
        return httpx.Response(429, text="Too Many Requests")

    client = _client_with_transport(handler)
    with pytest.raises(SPAPIError):
        client.get_pricing("B000000001")
