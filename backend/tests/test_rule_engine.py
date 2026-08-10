"""
Rule engine tests - pure logic, no DB/AWS needed (see engine.py's module
docstring on why it's a pure function). Required by CLAUDE.md build-order
step 12 ("Tests for matching logic and rule engine specifically") and
deliberately the most thorough test file in this codebase: this system's
outputs directly drive real purchase decisions, and CLAUDE.md is explicit
that correctness/explainability matter more than speed of delivery.

Every test asserts both the classification AND that the trace actually
explains it - a correct label with no reasoning would violate CLAUDE.md's
"owner must see why" requirement just as much as a wrong label.
"""

from __future__ import annotations

from decimal import Decimal

from adc_backend.modules.rules.config import DEFAULT_RULES_CONFIG
from adc_backend.modules.rules.engine import (
    ClassificationInputs,
    _required_margin_pct,
    _seller_count_risk,
    classify,
)
from adc_backend.modules.rules.models import ClassificationLabel

CONFIG = DEFAULT_RULES_CONFIG


def _inputs(**overrides) -> ClassificationInputs:
    """A solid BUY-shaped baseline: cost 10, sell 20, fees 5 -> profit 5 -> ROI 50%, margin 25%."""
    defaults = dict(
        unit_cost=Decimal(10),
        sell_price=Decimal(20),
        referral_fee=Decimal(3),
        fba_fee=Decimal(2),
        other_fees_total=Decimal(0),
        is_restricted=False,
        is_gated=False,
        gated_approval_status="not_applicable",
        manufacturer_sells_directly=False,
        amazon_has_buy_box=False,
        seller_count=2,
        sales_rank_drops_30d=None,
    )
    defaults.update(overrides)
    return ClassificationInputs(**defaults)


def _trace_rules(decision) -> dict[str, str]:
    """rule_name -> result, for easy trace assertions."""
    return {entry["rule"]: entry["result"] for entry in decision.rule_trace}


class TestHardExcludes:
    def test_restricted_is_no_buy_hard_exclude(self):
        decision = classify(_inputs(is_restricted=True), CONFIG)
        assert decision.classification == ClassificationLabel.NO_BUY
        assert _trace_rules(decision)["restricted_product"] == "hard_exclude"
        assert decision.roi_pct is None  # short-circuited before any financial math

    def test_manufacturer_sells_directly_is_no_buy_hard_exclude(self):
        decision = classify(_inputs(manufacturer_sells_directly=True), CONFIG)
        assert decision.classification == ClassificationLabel.NO_BUY
        assert _trace_rules(decision)["manufacturer_sells_directly"] == "hard_exclude"

    def test_restricted_takes_priority_over_manufacturer_check(self):
        # Both true - restricted is evaluated first and short-circuits, so
        # the manufacturer rule should never even appear in the trace.
        decision = classify(_inputs(is_restricted=True, manufacturer_sells_directly=True), CONFIG)
        assert "manufacturer_sells_directly" not in _trace_rules(decision)


class TestMissingData:
    def test_missing_cost_is_review_not_a_guess(self):
        decision = classify(_inputs(unit_cost=None), CONFIG)
        assert decision.classification == ClassificationLabel.REVIEW
        assert decision.roi_pct is None

    def test_missing_sell_price_is_review_not_a_guess(self):
        decision = classify(_inputs(sell_price=None), CONFIG)
        assert decision.classification == ClassificationLabel.REVIEW

    def test_zero_cost_is_review_not_a_divide_by_zero(self):
        decision = classify(_inputs(unit_cost=Decimal(0)), CONFIG)
        assert decision.classification == ClassificationLabel.REVIEW


class TestGating:
    def test_gated_and_approved_treated_as_normal(self):
        decision = classify(_inputs(is_gated=True, gated_approval_status="approved"), CONFIG)
        assert decision.classification == ClassificationLabel.BUY
        assert _trace_rules(decision)["gated_product"] == "info"

    def test_gated_pending_weak_deal_is_no_buy_not_excluded_from_review(self):
        # ROI 22% clears the normal 20% threshold but not 1.5x it (30%) -
        # not "notably strong" per CLAUDE.md, so NO_BUY rather than surfaced.
        decision = classify(
            _inputs(
                is_gated=True,
                gated_approval_status="not_approved",
                unit_cost=Decimal(10),
                sell_price=Decimal("12.2"),
                referral_fee=Decimal(0),
                fba_fee=Decimal(0),
            ),
            CONFIG,
        )
        assert decision.classification == ClassificationLabel.NO_BUY
        assert _trace_rules(decision)["gated_pending_strength_check"] == "fail"

    def test_gated_pending_strong_deal_is_review_not_no_buy(self):
        # ROI 35% clears 1.5x the 20% threshold (30%) - notably strong,
        # CLAUDE.md: surface it, flagged that it needs approval first.
        decision = classify(
            _inputs(
                is_gated=True,
                gated_approval_status="not_approved",
                unit_cost=Decimal(10),
                sell_price=Decimal("13.5"),
                referral_fee=Decimal(0),
                fba_fee=Decimal(0),
            ),
            CONFIG,
        )
        assert decision.classification == ClassificationLabel.REVIEW
        assert _trace_rules(decision)["gated_pending_strength_check"] == "pass"


class TestRoiAndMargin:
    def test_roi_below_negotiate_floor_is_no_buy(self):
        # cost 10, sell 10.5, no fees -> profit 0.5 -> ROI 5% (below the 10% floor)
        decision = classify(_inputs(sell_price=Decimal("10.5"), referral_fee=Decimal(0), fba_fee=Decimal(0)), CONFIG)
        assert decision.classification == ClassificationLabel.NO_BUY
        assert _trace_rules(decision)["roi_threshold"] == "fail"

    def test_roi_in_negotiate_band_is_negotiate_when_margin_still_clears(self):
        # cost 100, fees 10, profit 15 -> ROI 15% (10-20 band); sell 125 puts
        # it in the >50 margin tier (8% required) so margin still clears at 12%.
        decision = classify(
            _inputs(unit_cost=Decimal(100), sell_price=Decimal(125), referral_fee=Decimal(10), fba_fee=Decimal(0)),
            CONFIG,
        )
        assert decision.classification == ClassificationLabel.NEGOTIATE
        assert _trace_rules(decision)["roi_threshold"] == "info"

    def test_roi_clears_but_margin_fails_is_no_buy(self):
        # cost 10, fees 15, profit 2.5 -> ROI 25% (clears 20%), sell 27.5 ->
        # margin 9.09%, required 10% for the 15-50 tier - margin fails.
        decision = classify(
            _inputs(unit_cost=Decimal(10), sell_price=Decimal("27.5"), referral_fee=Decimal(15), fba_fee=Decimal(0)),
            CONFIG,
        )
        assert decision.classification == ClassificationLabel.NO_BUY
        assert _trace_rules(decision)["roi_threshold"] == "pass"
        assert _trace_rules(decision)["margin_threshold"] == "fail"

    def test_roi_and_margin_both_clear_is_buy(self):
        decision = classify(_inputs(), CONFIG)
        assert decision.classification == ClassificationLabel.BUY
        assert decision.roi_pct == Decimal(50)
        assert decision.margin_pct == Decimal(25)


class TestRiskFactors:
    def test_amazon_buy_box_is_high_risk_not_excluded(self):
        decision = classify(_inputs(amazon_has_buy_box=True), CONFIG)
        assert decision.classification == ClassificationLabel.HIGH_RISK
        assert _trace_rules(decision)["amazon_has_buy_box"] == "fail"

    def test_high_seller_count_is_high_risk(self):
        decision = classify(_inputs(seller_count=15), CONFIG)  # > medium_risk_max (8)
        assert decision.classification == ClassificationLabel.HIGH_RISK
        assert _trace_rules(decision)["seller_count_high_risk"] == "fail"

    def test_low_seller_count_does_not_trigger_high_risk(self):
        decision = classify(_inputs(seller_count=2), CONFIG)
        assert decision.classification != ClassificationLabel.HIGH_RISK

    def test_weak_velocity_is_review(self):
        decision = classify(_inputs(sales_rank_drops_30d=5), CONFIG)  # < min (40)
        assert decision.classification == ClassificationLabel.REVIEW
        assert _trace_rules(decision)["sales_velocity"] == "fail"

    def test_strong_velocity_is_buy(self):
        decision = classify(_inputs(sales_rank_drops_30d=100), CONFIG)
        assert decision.classification == ClassificationLabel.BUY
        assert _trace_rules(decision)["sales_velocity"] == "pass"

    def test_missing_velocity_data_does_not_block_buy(self):
        decision = classify(_inputs(sales_rank_drops_30d=None), CONFIG)
        assert decision.classification == ClassificationLabel.BUY
        assert _trace_rules(decision)["sales_velocity"] == "info"


class TestTraceCompleteness:
    def test_every_decision_has_a_non_empty_trace(self):
        for inputs in [
            _inputs(is_restricted=True),
            _inputs(unit_cost=None),
            _inputs(sell_price=Decimal("10.5"), referral_fee=Decimal(0), fba_fee=Decimal(0)),
            _inputs(amazon_has_buy_box=True),
            _inputs(),
        ]:
            decision = classify(inputs, CONFIG)
            assert len(decision.rule_trace) > 0
            for entry in decision.rule_trace:
                assert entry["rule"]
                assert entry["reasoning"]
                assert entry["result"] in ("pass", "fail", "hard_exclude", "info")


class TestMarginExceptionTable:
    def test_low_price_requires_higher_margin(self):
        assert _required_margin_pct(Decimal(10), CONFIG["margin_exception_table"]) == 15.0

    def test_mid_price_tier(self):
        assert _required_margin_pct(Decimal(30), CONFIG["margin_exception_table"]) == 10.0

    def test_high_price_falls_to_catch_all(self):
        assert _required_margin_pct(Decimal(500), CONFIG["margin_exception_table"]) == 8.0

    def test_boundary_price_uses_the_tier_it_exactly_matches(self):
        assert _required_margin_pct(Decimal(15), CONFIG["margin_exception_table"]) == 15.0
        assert _required_margin_pct(Decimal("15.01"), CONFIG["margin_exception_table"]) == 10.0


class TestSellerCountRisk:
    def test_low_risk_boundary(self):
        assert _seller_count_risk(3, CONFIG) == "low"

    def test_medium_risk_boundary(self):
        assert _seller_count_risk(4, CONFIG) == "medium"
        assert _seller_count_risk(8, CONFIG) == "medium"

    def test_high_risk_above_medium_max(self):
        assert _seller_count_risk(9, CONFIG) == "high"

    def test_unknown_seller_count(self):
        assert _seller_count_risk(None, CONFIG) == "unknown"
