"""
Manual review routing: decides *whether* and *why* a line item needs a
human decision, per CLAUDE.md's explicit list - low-confidence matches,
display/bundle SKUs, tiered promo pricing blocks, gated-pending items
with a notably strong deal, and any genuinely ambiguous case. Pure
function of already-computed state (item type, match confidence,
classification) - this module doesn't decide any of those things itself,
it just reads the verdicts step 4/7/8 already reached and picks the right
reason (or no reason at all) for the queue.

Priority order matters when a row could be routed for more than one
reason (e.g. a bundle SKU that also has a bad column mapping) - the most
specific/actionable reason wins so the owner sees the real blocker first,
not a downstream symptom of it.
"""

from __future__ import annotations

from dataclasses import dataclass

from adc_backend.modules.ingestion.models import LineItemType, ParseStatus
from adc_backend.modules.matching.engine import LOW_CONFIDENCE_REVIEW_THRESHOLD
from adc_backend.modules.matching.models import MatchStatus
from adc_backend.modules.review.models import ReviewReason
from adc_backend.modules.rules.models import ClassificationLabel


@dataclass
class LineItemReviewState:
    item_type: LineItemType
    parse_status: ParseStatus
    match_status: MatchStatus | None  # None if not matched at all yet
    match_confidence: float | None
    classification: ClassificationLabel | None  # None if not classified yet (e.g. bundle/promo skip the rule engine)
    classification_trace: list[dict] | None


def determine_review_reason(state: LineItemReviewState) -> tuple[ReviewReason, str] | None:
    """Returns (reason, notes) if this item belongs in the review queue, else None."""

    if state.item_type == LineItemType.DISPLAY_BUNDLE:
        return ReviewReason.DISPLAY_BUNDLE_SKU, "Display/bundle SKU - not eligible for normal per-unit ASIN matching."

    if state.item_type == LineItemType.TIERED_PROMO:
        return ReviewReason.TIERED_PROMO_PRICING, "Tiered promotional pricing block - not auto-parsed into a standard cost."

    if state.item_type == LineItemType.AMBIGUOUS or state.parse_status == ParseStatus.ERROR:
        return ReviewReason.AMBIGUOUS_PARSE, "Could not confidently parse this row after column mapping."

    if state.match_status == MatchStatus.REJECTED:
        # A rejected match's old confidence score is stale/irrelevant - the
        # item now has no usable match at all, full stop, regardless of
        # what the discarded suggestion used to score.
        return ReviewReason.LOW_CONFIDENCE_MATCH, "Previous ASIN match was rejected - needs a new match before this can be classified."

    if state.match_status == MatchStatus.PROPOSED and (
        state.match_confidence is None or state.match_confidence < LOW_CONFIDENCE_REVIEW_THRESHOLD
    ):
        confidence_text = f"{state.match_confidence:.0%}" if state.match_confidence is not None else "no match found"
        return ReviewReason.LOW_CONFIDENCE_MATCH, f"Proposed ASIN match confidence is {confidence_text} - below the review threshold."

    if state.classification == ClassificationLabel.REVIEW:
        if state.classification_trace and any(
            entry.get("rule") == "gated_pending_strength_check" and entry.get("result") == "pass"
            for entry in state.classification_trace
        ):
            return (
                ReviewReason.GATED_PENDING_STRONG_DEAL,
                (
                    "Gated and not yet approved, but ROI/margin are strong enough to be worth pursuing - "
                    "owner must submit an invoice to Amazon and await approval before this is purchasable."
                ),
            )
        return ReviewReason.OTHER, "Rule engine returned REVIEW - see the classification's rule_trace for the specific reason."

    return None
