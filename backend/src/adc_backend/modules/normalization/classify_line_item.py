"""
Line-item type classification: distinguishes normal per-unit rows from the
two special cases CLAUDE.md requires distinct handling for - display/
bundle SKUs and tiered promotional pricing blocks - plus a catch-all
"ambiguous" for anything that doesn't parse cleanly. None of these three
get auto-excluded or force-parsed; they route to manual review (step 9).

These are pattern-based heuristics, not a claim of perfect detection -
CLAUDE.md itself frames these as needing manual review specifically
because they're not reliably machine-parseable. The bar here is "catch the
obvious cases and don't crash/misparse on the rest," not "never miss one."
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from adc_backend.modules.ingestion.models import LineItemType, ParseStatus

BUNDLE_KEYWORDS = [
    "DISPLAY",
    "ASSORTMENT",
    "COUNTER DISPLAY",
    "PDQ",  # industry term for a pre-packed point-of-purchase display
    "KIT",
    "BUNDLE",
]

# Matches a dollar amount like "$2.90" or "2.90" preceded/followed by
# pricing punctuation - used to count how many distinct price points
# appear in one cell (tiered promo blocks pack several into one string).
_PRICE_TOKEN_RE = re.compile(r"\$?\s*\d+(?:,\d{3})*\.\d{2}")
_TIER_ARROW_RE = re.compile(r"->|→|=>")  # "->", "→", "=>"


@dataclass
class ClassificationOutcome:
    item_type: LineItemType
    parse_status: ParseStatus
    notes: str | None
    unit_price: Decimal | None  # only set for STANDARD items with a cleanly parsed price


def _contains_bundle_keyword(*texts: str | None) -> bool:
    for text in texts:
        if not text:
            continue
        upper = text.upper()
        if any(keyword in upper for keyword in BUNDLE_KEYWORDS):
            return True
    return False


def _looks_like_tiered_promo(price_text: str | None) -> bool:
    if not price_text:
        return False
    price_count = len(_PRICE_TOKEN_RE.findall(price_text))
    if price_count >= 2:
        return True
    return _TIER_ARROW_RE.search(price_text) is not None and price_count >= 1


def parse_price(price_text: str | None) -> Decimal | None:
    """Strips $ / commas / whitespace and parses a single price. None if not a clean single number."""
    if not price_text:
        return None
    cleaned = price_text.replace("$", "").replace(",", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def classify_line_item(
    *,
    description: str | None,
    raw_price_text: str | None,
    supplier_item_number: str | None,
) -> ClassificationOutcome:
    if _contains_bundle_keyword(description):
        return ClassificationOutcome(
            item_type=LineItemType.DISPLAY_BUNDLE,
            parse_status=ParseStatus.NEEDS_REVIEW,
            notes="Description matched a display/bundle keyword - not eligible for normal per-unit ASIN matching.",
            unit_price=None,
        )

    if _looks_like_tiered_promo(raw_price_text):
        return ClassificationOutcome(
            item_type=LineItemType.TIERED_PROMO,
            parse_status=ParseStatus.NEEDS_REVIEW,
            notes=f"Price cell {raw_price_text!r} looks like a tiered promotional block - not auto-parsed.",
            unit_price=None,
        )

    if not supplier_item_number:
        return ClassificationOutcome(
            item_type=LineItemType.AMBIGUOUS,
            parse_status=ParseStatus.ERROR,
            notes="No supplier item number after column mapping - can't identify this product.",
            unit_price=None,
        )

    price = parse_price(raw_price_text)
    if raw_price_text and price is None:
        return ClassificationOutcome(
            item_type=LineItemType.AMBIGUOUS,
            parse_status=ParseStatus.NEEDS_REVIEW,
            notes=f"Price cell {raw_price_text!r} didn't parse as a single number.",
            unit_price=None,
        )

    return ClassificationOutcome(
        item_type=LineItemType.STANDARD,
        parse_status=ParseStatus.OK,
        notes=None,
        unit_price=price,
    )
