"""
Matching engine tests. Pure-logic scoring/ranking tests need nothing;
persistence tests need real local Postgres (product_matches' unique
constraint and update-in-place behavior are exactly what's being verified,
so a real DB matters here, not a mock). Required by CLAUDE.md build-order
step 12 ("Tests for matching logic and rule engine specifically").
"""

from __future__ import annotations

import os
import uuid

import pytest

from adc_backend.modules.amazon.sp_api_client import CatalogMatch
from adc_backend.modules.matching.engine import (
    MIN_PROPOSAL_CONFIDENCE,
    rank_candidates,
    score_candidate,
)

# --- pure scoring/ranking logic - no DB needed ---


class TestScoreCandidate:
    def test_exact_title_and_brand_match_scores_highest(self):
        candidate = CatalogMatch(asin="B1", title="Blue Widget 10-Pack", brand="Acme")
        score = score_candidate("Blue Widget 10-Pack", "Acme", candidate)
        assert score > 0.95

    def test_completely_unrelated_title_scores_low(self):
        candidate = CatalogMatch(asin="B1", title="Red Garden Hose", brand="Acme")
        score = score_candidate("Blue Widget 10-Pack", "Acme", candidate)
        assert score < 0.4

    def test_brand_mismatch_scores_lower_than_brand_match(self):
        same_brand = score_candidate(
            "Blue Widget", "Acme", CatalogMatch(asin="B1", title="Blue Widget", brand="Acme")
        )
        diff_brand = score_candidate(
            "Blue Widget", "Acme", CatalogMatch(asin="B2", title="Blue Widget", brand="OtherCo")
        )
        assert same_brand > diff_brand

    def test_no_brand_on_either_side_is_capped_below_pure_title_match(self):
        candidate = CatalogMatch(asin="B1", title="Blue Widget", brand=None)
        score = score_candidate("Blue Widget", None, candidate)
        assert score == pytest.approx(0.85, abs=0.01)  # exact title, but capped since brand couldn't be cross-checked

    def test_supplier_gave_brand_but_amazon_listing_has_none_is_penalized(self):
        with_check = score_candidate("Blue Widget", None, CatalogMatch(asin="B1", title="Blue Widget", brand=None))
        without_check = score_candidate(
            "Blue Widget", "Acme", CatalogMatch(asin="B1", title="Blue Widget", brand=None)
        )
        assert without_check < with_check


class TestRankCandidates:
    def test_sorts_best_match_first(self):
        candidates = [
            CatalogMatch(asin="LOW", title="Totally Different Product", brand="Other"),
            CatalogMatch(asin="HIGH", title="Blue Widget 10-Pack", brand="Acme"),
        ]
        ranked = rank_candidates("Blue Widget 10-Pack", "Acme", candidates)
        assert ranked[0].candidate.asin == "HIGH"
        assert ranked[0].score > ranked[1].score

    def test_empty_candidates_returns_empty(self):
        assert rank_candidates("anything", None, []) == []


# --- persistence / instant-reuse behavior - needs real Postgres ---


class _StubSPAPIClient:
    """Duck-typed stand-in for SPAPIClient - raises if called when a test expects it NOT to be (the instant-reuse guarantee)."""

    def __init__(self, candidates: list[CatalogMatch] | None = None, should_not_be_called: bool = False):
        self._candidates = candidates or []
        self._should_not_be_called = should_not_be_called
        self.call_count = 0

    def search_catalog_items(self, keywords: str, marketplace_id: str = "", brand: str | None = None):
        if self._should_not_be_called:
            raise AssertionError("SP-API should not have been called - a CONFIRMED match should short-circuit this")
        self.call_count += 1
        return self._candidates


@pytest.fixture
def db_session():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set - see backend/docker-compose.yml")
    from adc_backend.db import models  # noqa: F401
    from adc_backend.db.base import get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _make_supplier(db_session):
    from adc_backend.db.core_models import Supplier

    supplier = Supplier(name=f"Test Supplier {uuid.uuid4()}", code=f"test-{uuid.uuid4().hex[:8]}")
    db_session.add(supplier)
    db_session.flush()
    return supplier


def test_confirmed_match_short_circuits_without_calling_sp_api(db_session):
    from adc_backend.modules.matching.engine import find_or_propose_match
    from adc_backend.modules.matching.models import MatchSource, MatchStatus, ProductMatch

    supplier = _make_supplier(db_session)
    confirmed = ProductMatch(
        supplier_id=supplier.id,
        supplier_item_number="ABC-1",
        asin="B0CONFIRMED",
        match_status=MatchStatus.CONFIRMED,
        match_source=MatchSource.MANUAL,
    )
    db_session.add(confirmed)
    db_session.flush()

    stub = _StubSPAPIClient(should_not_be_called=True)
    result = find_or_propose_match(
        db_session,
        stub,
        supplier_id=supplier.id,
        supplier_item_number="ABC-1",
        description="anything",
        brand=None,
    )
    assert result.id == confirmed.id
    assert result.asin == "B0CONFIRMED"
    assert stub.call_count == 0


def test_no_existing_match_calls_sp_api_and_creates_proposed(db_session):
    from adc_backend.modules.matching.engine import find_or_propose_match
    from adc_backend.modules.matching.models import MatchSource, MatchStatus

    supplier = _make_supplier(db_session)
    stub = _StubSPAPIClient(candidates=[CatalogMatch(asin="B0MATCH", title="Blue Widget", brand="Acme")])

    result = find_or_propose_match(
        db_session,
        stub,
        supplier_id=supplier.id,
        supplier_item_number="ABC-2",
        description="Blue Widget",
        brand="Acme",
    )
    assert result.asin == "B0MATCH"
    assert result.match_status == MatchStatus.PROPOSED
    assert result.match_source == MatchSource.AUTO
    assert stub.call_count == 1


def test_existing_proposed_match_updated_in_place_not_duplicated(db_session):
    from sqlalchemy import select

    from adc_backend.modules.matching.engine import find_or_propose_match
    from adc_backend.modules.matching.models import ProductMatch

    supplier = _make_supplier(db_session)
    stub = _StubSPAPIClient(candidates=[CatalogMatch(asin="B0FIRST", title="Blue Widget", brand="Acme")])
    first = find_or_propose_match(
        db_session, stub, supplier_id=supplier.id, supplier_item_number="ABC-3", description="Blue Widget", brand="Acme"
    )

    stub2 = _StubSPAPIClient(candidates=[CatalogMatch(asin="B0SECOND", title="Blue Widget", brand="Acme")])
    second = find_or_propose_match(
        db_session, stub2, supplier_id=supplier.id, supplier_item_number="ABC-3", description="Blue Widget", brand="Acme"
    )

    assert first.id == second.id  # same row, updated - not a duplicate
    assert second.asin == "B0SECOND"

    all_rows = db_session.execute(
        select(ProductMatch).where(
            ProductMatch.supplier_id == supplier.id, ProductMatch.supplier_item_number == "ABC-3"
        )
    ).scalars().all()
    assert len(all_rows) == 1


def test_low_confidence_candidate_leaves_asin_null(db_session):
    from adc_backend.modules.matching.engine import find_or_propose_match

    supplier = _make_supplier(db_session)
    stub = _StubSPAPIClient(candidates=[CatalogMatch(asin="B0BAD", title="Completely Unrelated Item", brand="Nobody")])

    result = find_or_propose_match(
        db_session,
        stub,
        supplier_id=supplier.id,
        supplier_item_number="ABC-4",
        description="Blue Widget 10-Pack",
        brand="Acme",
    )
    assert result.asin is None
    assert result.match_confidence is not None  # recorded for audit even though it didn't clear the bar
    assert result.match_confidence < MIN_PROPOSAL_CONFIDENCE


def test_no_candidates_at_all_leaves_match_unset(db_session):
    from adc_backend.modules.matching.engine import find_or_propose_match

    supplier = _make_supplier(db_session)
    stub = _StubSPAPIClient(candidates=[])

    result = find_or_propose_match(
        db_session, stub, supplier_id=supplier.id, supplier_item_number="ABC-5", description="Blue Widget", brand=None
    )
    assert result.asin is None
    assert result.match_confidence is None


def test_confirm_match_sets_status_and_confirmed_by(db_session):
    from adc_backend.modules.matching.engine import confirm_match, find_or_propose_match
    from adc_backend.modules.matching.models import MatchStatus

    supplier = _make_supplier(db_session)
    stub = _StubSPAPIClient(candidates=[CatalogMatch(asin="B0MATCH", title="Blue Widget", brand="Acme")])
    proposed = find_or_propose_match(
        db_session, stub, supplier_id=supplier.id, supplier_item_number="ABC-6", description="Blue Widget", brand="Acme"
    )

    confirmed = confirm_match(db_session, proposed.id, confirmed_by="owner@example.com")
    assert confirmed.match_status == MatchStatus.CONFIRMED
    assert confirmed.confirmed_by == "owner@example.com"
    assert confirmed.confirmed_at is not None
    assert confirmed.asin == "B0MATCH"  # unchanged - no override given


def test_confirm_match_with_override_asin_becomes_manual(db_session):
    from adc_backend.modules.matching.engine import confirm_match, find_or_propose_match
    from adc_backend.modules.matching.models import MatchSource

    supplier = _make_supplier(db_session)
    stub = _StubSPAPIClient(candidates=[CatalogMatch(asin="B0WRONG", title="Blue Widget", brand="Acme")])
    proposed = find_or_propose_match(
        db_session, stub, supplier_id=supplier.id, supplier_item_number="ABC-7", description="Blue Widget", brand="Acme"
    )

    confirmed = confirm_match(db_session, proposed.id, confirmed_by="owner@example.com", asin="B0CORRECT")
    assert confirmed.asin == "B0CORRECT"
    assert confirmed.match_source == MatchSource.MANUAL
    assert confirmed.match_confidence is None  # a manual override has no fuzzy-match score to report


def test_reject_match_sets_status(db_session):
    from adc_backend.modules.matching.engine import find_or_propose_match, reject_match
    from adc_backend.modules.matching.models import MatchStatus

    supplier = _make_supplier(db_session)
    stub = _StubSPAPIClient(candidates=[CatalogMatch(asin="B0MATCH", title="Blue Widget", brand="Acme")])
    proposed = find_or_propose_match(
        db_session, stub, supplier_id=supplier.id, supplier_item_number="ABC-8", description="Blue Widget", brand="Acme"
    )

    rejected = reject_match(db_session, proposed.id, confirmed_by="owner@example.com")
    assert rejected.match_status == MatchStatus.REJECTED
