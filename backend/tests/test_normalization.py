"""Tests for column mapping proposal and line-item classification - pure logic, no DB/AWS needed."""

from __future__ import annotations

from decimal import Decimal

from adc_backend.modules.ingestion.models import LineItemType, ParseStatus
from adc_backend.modules.normalization.classify_line_item import classify_line_item, parse_price
from adc_backend.modules.normalization.column_mapping import propose_column_mapping


class TestColumnMapping:
    def test_maps_inconsistent_headers_to_canonical_fields(self):
        # The exact CLAUDE.md example: "SALE PRICE" instead of "UNIT PRICE".
        headers = ["ITEM #", "DESCRIPTION", "SALE PRICE", "CASE QTY", "UNIT"]
        mapping = propose_column_mapping(headers)

        assert mapping["supplier_item_number"].header == "ITEM #"
        assert mapping["description"].header == "DESCRIPTION"
        assert mapping["unit_price"].header == "SALE PRICE"
        assert mapping["case_quantity"].header == "CASE QTY"
        assert mapping["case_unit"].header == "UNIT"
        for field in mapping.values():
            assert 0.0 <= field.confidence <= 1.0

    def test_noise_column_from_prior_order_form_not_mapped(self):
        # CLAUDE.md's explicit example of a noise column that must NOT be
        # mistaken for a real field.
        headers = ["ITEM #", "DESCRIPTION", "UNIT PRICE", "QTY ORDERED"]
        mapping = propose_column_mapping(headers)
        assert mapping["case_quantity"].header != "QTY ORDERED"

    def test_each_header_claimed_at_most_once(self):
        headers = ["ITEM #", "DESCRIPTION", "PRICE"]
        mapping = propose_column_mapping(headers)
        claimed = [fm.header for fm in mapping.values() if fm.header]
        assert len(claimed) == len(set(claimed))

    def test_unrecognized_headers_left_unmapped(self):
        headers = ["XYZZY123", "FOOBAR456"]
        mapping = propose_column_mapping(headers)
        assert all(fm.header is None for fm in mapping.values())


class TestClassifyLineItem:
    def test_standard_item_parses_cleanly(self):
        outcome = classify_line_item(
            description="Widget, Blue, 10-pack",
            raw_price_text="$2.90",
            supplier_item_number="ABC-1",
        )
        assert outcome.item_type == LineItemType.STANDARD
        assert outcome.parse_status == ParseStatus.OK
        assert outcome.unit_price == Decimal("2.90")

    def test_display_bundle_keyword_routes_to_review_not_excluded(self):
        outcome = classify_line_item(
            description="9 COLOR COUNTER DISPLAY",
            raw_price_text="45.00",
            supplier_item_number="DISP-9",
        )
        assert outcome.item_type == LineItemType.DISPLAY_BUNDLE
        assert outcome.parse_status == ParseStatus.NEEDS_REVIEW
        assert outcome.unit_price is None  # not auto-parsed into a per-unit cost

    def test_tiered_promo_pricing_block_not_auto_parsed(self):
        # The exact CLAUDE.md example.
        outcome = classify_line_item(
            description="Widget",
            raw_price_text="1 pc = $2.90 -> $1.45, 180 pcs = $208.80/DP",
            supplier_item_number="ABC-2",
        )
        assert outcome.item_type == LineItemType.TIERED_PROMO
        assert outcome.parse_status == ParseStatus.NEEDS_REVIEW
        assert outcome.unit_price is None

    def test_missing_supplier_item_number_is_error(self):
        outcome = classify_line_item(description="Widget", raw_price_text="$2.90", supplier_item_number=None)
        assert outcome.parse_status == ParseStatus.ERROR

    def test_unparseable_price_is_ambiguous_not_guessed(self):
        outcome = classify_line_item(
            description="Widget", raw_price_text="call for pricing", supplier_item_number="ABC-3"
        )
        assert outcome.item_type == LineItemType.AMBIGUOUS
        assert outcome.parse_status == ParseStatus.NEEDS_REVIEW
        assert outcome.unit_price is None

    def test_no_price_at_all_is_standard_with_null_price(self):
        # A missing price cell isn't the same as an unparseable one - don't
        # manufacture an "ambiguous" flag for a column that was just empty.
        outcome = classify_line_item(description="Widget", raw_price_text=None, supplier_item_number="ABC-4")
        assert outcome.item_type == LineItemType.STANDARD
        assert outcome.parse_status == ParseStatus.OK
        assert outcome.unit_price is None


def test_parse_price_handles_dollar_sign_and_commas():
    assert parse_price("$1,234.56") == Decimal("1234.56")
    assert parse_price("2.90") == Decimal("2.90")
    assert parse_price(None) is None
    assert parse_price("call for pricing") is None
