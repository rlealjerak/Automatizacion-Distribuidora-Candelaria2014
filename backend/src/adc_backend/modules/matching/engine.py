"""
Matching engine: resolves a supplier line item to an Amazon ASIN.

Once the owner confirms a match it's saved permanently in
`product_matches` (build-order step 2's schema) and reused instantly on
future runs from that supplier, at high confidence, without calling
SP-API again. This module is the "instantly" part: check the persistent
mapping first, only call the catalog search when there's nothing
confirmed to reuse.

UPC-first matching (added 2026-10-11): CLAUDE.md's "no supplier ever
provides UPC/EAN" was disproved by the real MTZ price list. When a valid
UPC/EAN is given (check-digit validated, not just the right digit count -
see modules/matching/upc.py), it's tried FIRST via SP-API's identifier
lookup - an exact barcode match is strictly more reliable than fuzzy
text/brand scoring:
  - exactly one ASIN back -> high confidence (1.0), MatchSource.UPC.
  - zero ASINs back -> falls through to text/brand fuzzy matching below
    (an unrecognized barcode isn't evidence of anything, it's just no
    answer from this path).
  - more than one ASIN back -> genuinely ambiguous (the same barcode
    resolving to multiple real Amazon listings - happens with
    reused/regional barcodes) - asin stays null, every candidate is
    recorded on candidate_asins, and review routing (see
    modules/review/routing.py) sends this to the owner rather than
    picking one.
Text/brand fuzzy matching (the original, still-default path) only runs
when there's no UPC, the UPC fails check-digit validation, or the UPC
lookup found nothing.

This engine never sets a match to CONFIRMED itself - only the owner does,
via `confirm_match` (called from the tool interface, step 10). Everything
this engine produces on its own is PROPOSED at best.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from adc_backend.modules.amazon.sp_api_client import CatalogMatch, SPAPIClient
from adc_backend.modules.matching.description_cleanup import clean_description_for_matching
from adc_backend.modules.matching.models import MatchSource, MatchStatus, ProductMatch
from adc_backend.modules.matching.upc import identifier_type_for_upc, validate_upc_ean_check_digit

# A UPC-first match is an exact barcode hit, not a fuzzy guess - full
# confidence, same scale as the fuzzy scorer's 0.0-1.0.
UPC_MATCH_CONFIDENCE = 1.0

# Below this, a proposed match isn't worth acting on even as a suggestion -
# leave asin null and let the review queue (step 9) route it as a genuine
# unmatched/ambiguous case rather than showing a misleading low-confidence guess.
MIN_PROPOSAL_CONFIDENCE = 0.40

# At or above this, a *proposed* (not yet owner-confirmed) match is
# reliable enough to not need to sit in the manual review queue just for
# being unconfirmed - step 9 uses this constant, matching engine doesn't
# act on it (never auto-confirms).
LOW_CONFIDENCE_REVIEW_THRESHOLD = 0.75


@dataclass
class ScoredCandidate:
    candidate: CatalogMatch
    score: float  # 0.0-1.0


def score_candidate(description: str, brand: str | None, candidate: CatalogMatch) -> float:
    """
    Title similarity is the base score; brand is a *modifier* on it, not an
    independent additive component. Deliberately multiplicative rather
    than `title*0.8 + brand*0.2` - an earlier additive version let a
    same-brand-but-completely-different-product candidate (e.g. supplier's
    "Blue Widget 10-Pack" vs. the same manufacturer's "Red Garden Hose")
    clear the proposal threshold on brand agreement alone, independent of
    how unrelated the titles actually were. A wrong same-brand suggestion
    is exactly the kind of confident-looking false positive this system
    can't afford - caught by test_matching_engine.py, not just reasoned
    about, so keep it multiplicative if this is touched again.
    """
    title_score = fuzz.token_sort_ratio(description.upper(), candidate.title.upper()) / 100.0

    if brand and candidate.brand:
        brand_similarity = fuzz.ratio(brand.upper(), candidate.brand.upper()) / 100.0
        modifier = 0.85 + 0.15 * brand_similarity  # ranges 0.85 (brand contradicts) to 1.0 (brand confirms)
        return min(1.0, title_score * modifier)
    if brand and not candidate.brand:
        # Supplier told us a brand, Amazon's catalog entry doesn't have
        # one to check it against - can't confirm, can't deny, slightly
        # bigger penalty than the no-brand-info-anywhere case below.
        return max(0.0, title_score * 0.80)
    return title_score * 0.85  # no brand to cross-check either side - cap below a full title-only match


def rank_candidates(description: str, brand: str | None, candidates: list[CatalogMatch]) -> list[ScoredCandidate]:
    scored = [ScoredCandidate(candidate=c, score=score_candidate(description, brand, c)) for c in candidates]
    return sorted(scored, key=lambda sc: sc.score, reverse=True)


def get_persistent_match(db: Session, supplier_id: uuid.UUID, supplier_item_number: str) -> ProductMatch | None:
    return db.execute(
        select(ProductMatch).where(
            ProductMatch.supplier_id == supplier_id,
            ProductMatch.supplier_item_number == supplier_item_number,
        )
    ).scalar_one_or_none()


@dataclass
class _ResolvedMatch:
    asin: str | None
    confidence: float | None
    match_source: MatchSource
    candidate_asins: list[str] | None = None


def _resolve_via_upc(sp_api_client: SPAPIClient, upc: str) -> _ResolvedMatch | None:
    """
    None means "no usable UPC path" (invalid, or zero hits) - the caller
    falls back to fuzzy matching in both cases. A non-None result always
    carries a real outcome: one ASIN, or multiple candidates for review.
    """
    if not validate_upc_ean_check_digit(upc):
        return None
    identifier_type = identifier_type_for_upc(upc.strip())
    if identifier_type is None:
        return None

    candidates = sp_api_client.search_catalog_items_by_identifier(upc.strip(), identifier_type)
    if not candidates:
        return None
    if len(candidates) == 1:
        return _ResolvedMatch(asin=candidates[0].asin, confidence=UPC_MATCH_CONFIDENCE, match_source=MatchSource.UPC)
    return _ResolvedMatch(
        asin=None,
        confidence=None,
        match_source=MatchSource.UPC,
        candidate_asins=[c.asin for c in candidates],
    )


def _resolve_via_fuzzy_text(
    sp_api_client: SPAPIClient, description: str, brand: str | None
) -> _ResolvedMatch:
    cleaned_description = clean_description_for_matching(description)
    candidates = sp_api_client.search_catalog_items(keywords=cleaned_description, brand=brand)
    ranked = rank_candidates(cleaned_description, brand, candidates)
    best = ranked[0] if ranked else None

    # Confidence is recorded even below the proposal threshold (useful for
    # audit/debugging "why didn't this match anything"), but asin is only
    # set once it clears the bar - a sub-threshold ASIN isn't worth showing.
    confidence = best.score if best else None
    asin = best.candidate.asin if best and best.score >= MIN_PROPOSAL_CONFIDENCE else None
    return _ResolvedMatch(asin=asin, confidence=confidence, match_source=MatchSource.AUTO)


def find_or_propose_match(
    db: Session,
    sp_api_client: SPAPIClient,
    *,
    supplier_id: uuid.UUID,
    supplier_item_number: str,
    description: str,
    brand: str | None,
    upc: str | None = None,
) -> ProductMatch:
    """
    The core "instant reuse" behavior: a CONFIRMED persistent match short-
    circuits before any SP-API call happens at all. Anything else (no
    match yet, or a previous PROPOSED/REJECTED one) triggers a fresh
    lookup and produces/updates a PROPOSED match for the owner to review -
    it does not overwrite a CONFIRMED match with a new guess.

    UPC-first: a valid upc is tried via identifier lookup before any
    fuzzy text/brand search runs at all. Only falls through to fuzzy
    matching when upc is None/invalid, or the identifier lookup itself
    found nothing - see module docstring.
    """
    existing = get_persistent_match(db, supplier_id, supplier_item_number)
    if existing is not None and existing.match_status == MatchStatus.CONFIRMED:
        return existing

    resolved = _resolve_via_upc(sp_api_client, upc) if upc else None
    if resolved is None:
        resolved = _resolve_via_fuzzy_text(sp_api_client, description, brand)

    if existing is not None:
        existing.asin = resolved.asin
        existing.match_confidence = resolved.confidence
        existing.match_status = MatchStatus.PROPOSED
        existing.match_source = resolved.match_source
        existing.candidate_asins = resolved.candidate_asins
        db.flush()
        return existing

    new_match = ProductMatch(
        supplier_id=supplier_id,
        supplier_item_number=supplier_item_number,
        asin=resolved.asin,
        match_confidence=resolved.confidence,
        match_status=MatchStatus.PROPOSED,
        match_source=resolved.match_source,
        candidate_asins=resolved.candidate_asins,
    )
    db.add(new_match)
    db.flush()
    return new_match


def confirm_match(db: Session, match_id: uuid.UUID, confirmed_by: str, asin: str | None = None) -> ProductMatch:
    """Owner approves a proposed match - or overrides it with their own ASIN (match_source becomes MANUAL)."""
    match = db.get(ProductMatch, match_id)
    if match is None:
        raise ValueError(f"No product_match with id {match_id}")
    if asin is not None and asin != match.asin:
        match.asin = asin
        match.match_source = MatchSource.MANUAL
        match.match_confidence = None  # a manual override has no fuzzy-match confidence score to report
    match.match_status = MatchStatus.CONFIRMED
    match.confirmed_by = confirmed_by
    match.confirmed_at = datetime.now(UTC)
    db.flush()
    return match


def reject_match(db: Session, match_id: uuid.UUID, confirmed_by: str) -> ProductMatch:
    match = db.get(ProductMatch, match_id)
    if match is None:
        raise ValueError(f"No product_match with id {match_id}")
    match.match_status = MatchStatus.REJECTED
    match.confirmed_by = confirmed_by
    match.confirmed_at = datetime.now(UTC)
    db.flush()
    return match
