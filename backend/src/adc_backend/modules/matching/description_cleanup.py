"""
Strips trailing supplier-catalog noise from a description before it's
fuzzy-scored against the Amazon catalog (score_candidate in engine.py).

Added 2026-10-11: the real MTZ price list's DESCRIPTION column ends
every row with its own internal item number, source country, and
case-pack text - e.g. "212 3.4 EDT L  (100010) - Spain - 20pcs ByBox".
None of that ever appears in an Amazon listing title, so leaving it in
only drags down token_sort_ratio against the real title. This only
affects the text/brand fallback path (UPC-first matching doesn't need
it) - see engine.py's find_or_propose_match.
"""

from __future__ import annotations

import re

# Matches "(123456) - Country - 20pcs ByBox" (and minor variants: extra
# spaces, "pc"/"pcs", with or without a trailing word after the pack
# count) anchored to the end of the string - never mid-string, so a
# product name that coincidentally contains a parenthesized number
# earlier isn't touched.
_TRAILING_NOISE_RE = re.compile(
    r"\s*\(\d+\)\s*-\s*[A-Za-z][A-Za-z\s]*-\s*\d+\s*p(?:cs|c)s?\b.*$",
    re.IGNORECASE,
)


def clean_description_for_matching(description: str) -> str:
    """Returns the description with trailing item-number/country/case-pack noise removed, for fuzzy scoring only - never stored back onto the raw line item."""
    if not description:
        return description
    cleaned = _TRAILING_NOISE_RE.sub("", description).strip()
    return cleaned or description.strip()
