"""
Confidence-scored column mapping proposal.

Supplier files use wildly inconsistent headers for the same concept
(CLAUDE.md: `UNIT PRICE` vs `SALE` vs `SALE PRICE`) and carry noise
columns left over from prior order forms (`QTY ORDERED`). This proposes a
best-guess mapping from each canonical field this system needs to one of
the source file's actual headers, with a confidence score - it never
silently applies itself. The owner confirms (or corrects) it before
anything downstream reads from it (build-order step 4's explicit
requirement).
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

# Canonical field -> known header aliases seen across suppliers. Order
# doesn't matter - every alias is scored, the best one wins. Deliberately
# specific (not just "QTY") so known noise columns like "QTY ORDERED"
# (leftover from prior order forms, per CLAUDE.md) don't accidentally
# fuzzy-match a real field - "ORDERED" isn't close enough to "PER CASE"/
# "CASE PACK" phrasing to win.
FIELD_ALIASES: dict[str, list[str]] = {
    "supplier_item_number": [
        "ITEM #",
        "ITEM NO",
        "ITEM NUMBER",
        "ITEM CODE",
        "SKU",
        "SUPPLIER ITEM",
        "SUPPLIER ITEM NUMBER",
        "PRODUCT CODE",
        "PART #",
        "PART NUMBER",
        "MODEL #",
    ],
    "description": [
        "DESCRIPTION",
        "DESC",
        "PRODUCT DESCRIPTION",
        "ITEM DESCRIPTION",
        "PRODUCT NAME",
        "PRODUCT",
        "ITEM NAME",
    ],
    "brand": ["BRAND", "MANUFACTURER", "MFG", "MFR", "BRAND NAME"],
    "unit_price": [
        "UNIT PRICE",
        "SALE",
        "SALE PRICE",
        "PRICE",
        "COST",
        "UNIT COST",
        "WHOLESALE PRICE",
        "WHOLESALE COST",
        "OUR PRICE",
    ],
    "case_quantity": [
        "CASE QTY",
        "CASE QUANTITY",
        "QTY PER CASE",
        "CASE PACK",
        "PACK SIZE",
        "UNITS PER CASE",
        "PACK QTY",
    ],
    "case_unit": ["UNIT", "UOM", "UNIT OF MEASURE", "CASE UNIT", "PACK TYPE", "PACK UNIT"],
}

# Below this score (0-100, rapidfuzz scale), a field is left unmapped
# rather than guessed - the owner fills it in manually. Chosen conservatively:
# false "confident" mappings are worse than an honest blank, since a wrong
# auto-mapping silently corrupts every downstream number for that column.
MIN_CONFIDENCE = 60.0

# Headers containing any of these are never mapped to anything, full stop -
# regardless of fuzzy score. CLAUDE.md's own example, "QTY ORDERED" (a
# leftover column from a prior order form), scores 61% against "CASE QTY"
# under WRatio purely because both contain "QTY" - a real failure mode
# tuning the confidence threshold alone can't fix (raising it enough to
# exclude this also starts rejecting genuine matches). An explicit denylist
# for known noise-column language is more honest than chasing a numeric
# threshold that happens to work on today's test cases.
NOISE_HEADER_KEYWORDS = ["ORDERED", "PREV ORDER", "LAST ORDER", "BACKORDER"]


@dataclass
class FieldMapping:
    header: str | None  # the source file's actual header, or None if nothing scored well enough
    confidence: float  # 0.0-1.0


def propose_column_mapping(headers: list[str]) -> dict[str, FieldMapping]:
    """
    Greedy best-match assignment: score every (canonical field, header)
    pair, then assign highest-scoring pairs first, skipping any field or
    header already claimed. Guarantees each header maps to at most one
    field and vice versa - two canonical fields can't both claim the same
    source column.
    """
    candidates: list[tuple[float, str, str]] = []  # (score, field, header)
    for field, aliases in FIELD_ALIASES.items():
        for header in headers:
            header_upper = header.upper().strip()
            if any(noise in header_upper for noise in NOISE_HEADER_KEYWORDS):
                continue
            best_alias_score = max(fuzz.WRatio(header_upper, alias) for alias in aliases)
            candidates.append((best_alias_score, field, header))

    candidates.sort(key=lambda c: c[0], reverse=True)

    result: dict[str, FieldMapping] = {field: FieldMapping(header=None, confidence=0.0) for field in FIELD_ALIASES}
    claimed_headers: set[str] = set()
    claimed_fields: set[str] = set()

    for score, field, header in candidates:
        if field in claimed_fields or header in claimed_headers:
            continue
        if score < MIN_CONFIDENCE:
            continue
        result[field] = FieldMapping(header=header, confidence=round(score / 100.0, 4))
        claimed_fields.add(field)
        claimed_headers.add(header)

    return result


def mapping_to_json(mapping: dict[str, FieldMapping]) -> dict:
    """Serializable form for ListRun.proposed_column_mapping / confirmed_column_mapping (JSONB)."""
    return {field: {"header": fm.header, "confidence": fm.confidence} for field, fm in mapping.items()}
