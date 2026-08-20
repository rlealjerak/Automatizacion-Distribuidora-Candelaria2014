"""Manual review routing tests - pure logic, no DB needed."""

from __future__ import annotations

from adc_backend.modules.ingestion.models import LineItemType, ParseStatus
from adc_backend.modules.matching.models import MatchStatus
from adc_backend.modules.review.models import ReviewReason
from adc_backend.modules.review.routing import LineItemReviewState, determine_review_reason
from adc_backend.modules.rules.models import ClassificationLabel


def _state(**overrides) -> LineItemReviewState:
    defaults = dict(
        item_type=LineItemType.STANDARD,
        parse_status=ParseStatus.OK,
        match_status=MatchStatus.CONFIRMED,
        match_confidence=0.95,
        classification=ClassificationLabel.BUY,
        classification_trace=None,
    )
    defaults.update(overrides)
    return LineItemReviewState(**defaults)


def test_clean_confirmed_buy_needs_no_review():
    assert determine_review_reason(_state()) is None


def test_display_bundle_routes_to_review_even_if_otherwise_clean():
    reason, _ = determine_review_reason(_state(item_type=LineItemType.DISPLAY_BUNDLE))
    assert reason == ReviewReason.DISPLAY_BUNDLE_SKU


def test_tiered_promo_routes_to_review():
    reason, _ = determine_review_reason(_state(item_type=LineItemType.TIERED_PROMO))
    assert reason == ReviewReason.TIERED_PROMO_PRICING


def test_ambiguous_item_type_routes_to_review():
    reason, _ = determine_review_reason(_state(item_type=LineItemType.AMBIGUOUS))
    assert reason == ReviewReason.AMBIGUOUS_PARSE


def test_parse_error_routes_to_review_even_if_item_type_standard():
    reason, _ = determine_review_reason(_state(parse_status=ParseStatus.ERROR))
    assert reason == ReviewReason.AMBIGUOUS_PARSE


def test_unconfirmed_low_confidence_match_routes_to_review():
    reason, notes = determine_review_reason(
        _state(match_status=MatchStatus.PROPOSED, match_confidence=0.5, classification=None)
    )
    assert reason == ReviewReason.LOW_CONFIDENCE_MATCH
    assert "50%" in notes


def test_unconfirmed_high_confidence_match_does_not_need_review_on_match_grounds_alone():
    # PROPOSED but confidence clears the threshold, and nothing else is
    # wrong - not flagged for the match itself (still not auto-confirmed,
    # but not blocking review-queue-wise either).
    result = determine_review_reason(
        _state(match_status=MatchStatus.PROPOSED, match_confidence=0.9, classification=None)
    )
    assert result is None


def test_no_match_attempted_yet_does_not_trigger_low_confidence_reason():
    # match_status None alone doesn't trigger LOW_CONFIDENCE_MATCH under
    # current logic (no match attempted isn't the same as a bad match) -
    # this documents that boundary explicitly rather than leaving it implicit.
    result = determine_review_reason(_state(match_status=None, match_confidence=None, classification=None))
    assert result is None


def test_rejected_match_status_routes_to_review():
    reason, _ = determine_review_reason(
        _state(match_status=MatchStatus.REJECTED, match_confidence=0.9, classification=None)
    )
    assert reason == ReviewReason.LOW_CONFIDENCE_MATCH


def test_gated_pending_strong_deal_gets_specific_reason():
    trace = [{"rule": "gated_pending_strength_check", "result": "pass", "reasoning": "strong"}]
    reason, notes = determine_review_reason(
        _state(classification=ClassificationLabel.REVIEW, classification_trace=trace)
    )
    assert reason == ReviewReason.GATED_PENDING_STRONG_DEAL
    assert "invoice" in notes.lower()


def test_ambiguous_restriction_gets_specific_reason():
    trace = [{"rule": "ambiguous_restriction_reason", "result": "info", "reasoning": "NOT_ELIGIBLE"}]
    reason, notes = determine_review_reason(
        _state(classification=ClassificationLabel.REVIEW, classification_trace=trace)
    )
    assert reason == ReviewReason.AMBIGUOUS_RESTRICTION
    assert "message text" in notes.lower()


def test_review_classification_without_gating_falls_back_to_other():
    trace = [{"rule": "sales_velocity", "result": "fail", "reasoning": "weak"}]
    reason, _ = determine_review_reason(
        _state(classification=ClassificationLabel.REVIEW, classification_trace=trace)
    )
    assert reason == ReviewReason.OTHER


def test_bundle_takes_priority_over_low_confidence_match():
    # Both conditions true - the more specific/actionable reason (bundle)
    # should win over the generic low-confidence-match symptom.
    reason, _ = determine_review_reason(
        _state(item_type=LineItemType.DISPLAY_BUNDLE, match_status=MatchStatus.PROPOSED, match_confidence=0.2)
    )
    assert reason == ReviewReason.DISPLAY_BUNDLE_SKU


def test_buy_classification_with_confirmed_match_needs_no_review():
    assert determine_review_reason(_state(classification=ClassificationLabel.BUY)) is None

    assert determine_review_reason(_state(classification=ClassificationLabel.HIGH_RISK)) is None
