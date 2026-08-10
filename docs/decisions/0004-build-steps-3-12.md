# 0004 - Build-order steps 3-12 (full MVP pipeline)

Date: 2026-08-06

## Context

Built at the user's explicit direction to implement the remainder of the
build order in one pass, departing from CLAUDE.md's normal "one reviewable
piece at a time, confirm before moving on" working style. Recorded here in
one doc (rather than one per step) since the decisions are interconnected
across the pipeline. See the final chat summary for the step-by-step
build narrative; this doc is the durable "why," matching 0001-0003's
purpose.

## The single most important caveat, stated once clearly

**SP-API and Keepa integration (steps 5-6) has never made a real network
call to either service.** Only placeholder Secrets Manager values exist -
no real SP-API refresh token/credentials, no real Keepa API key. Every
line of matching, pricing, fees, restrictions, and velocity logic that
depends on those clients is built to each API's publicly documented
contract and tested against hand-written mocks shaped like that
documentation - never against a recorded real response. CLAUDE.md's core
principle is "live data, deterministic logic"; what's been verified here
is the deterministic half. The first real SP-API/Keepa call, whenever
credentials are entered, is the actual verification of that half - not
something to assume from tests passing. `sp_api_client.py` and
`keepa_client.py` both restate this in their module docstrings so it
isn't lost the next time someone opens those files.

## Decisions

**PDF parsing uses pdfplumber's default table detection with a shared
first-non-empty-row-is-header heuristic**, same as CSV/Excel. Tested
against a real generated PDF table (via `reportlab`, a test-only
dependency), not asserted against nothing - but CLAUDE.md itself
describes real supplier PDFs as messy multi-column marketing layouts, and
no real sample files exist in this repo to validate against. Whatever
this heuristic gets wrong is exactly what manual review (step 9) exists
to catch - flagged in `parsers.py`'s docstring as a deliberate scope
boundary, not a gap to silently work around.

**Column-mapping confidence scoring needed an explicit noise-keyword
denylist, not just a numeric threshold.** `QTY ORDERED` (CLAUDE.md's own
example of a leftover noise column) scored 61% against `CASE QTY` under
rapidfuzz's `WRatio` - above the original 60% confidence floor - purely
because both strings contain "QTY". Caught by a test written directly
from CLAUDE.md's example, not by manual review of the code. Fixed with an
explicit `NOISE_HEADER_KEYWORDS` list (`ORDERED`, `BACKORDER`, ...) that
disqualifies a header from any mapping regardless of fuzzy score - more
honest than chasing a threshold that happens to work on today's test
cases. See `column_mapping.py`.

**`unit_lookup` seed data only marks EA/PCS/DZ as confirmed multipliers.**
CA/BX/DP/ST are seeded with `multiplier_confirmed=False` and a null
`units_per_case` - CLAUDE.md is explicit that DP/ST aren't standard units
and shouldn't be guessed at, and the same caution was extended to CA/BX
since case sizes vary by supplier in practice too.

**`AmazonDataSnapshot` gained `sales_rank_drops_30d`/`90d` columns after
the original step-2 schema design**, once building the Keepa client
(step 6) revealed Keepa doesn't report "units sold/month" as a field -
that number is something third-party tools model themselves, which this
system deliberately doesn't fabricate. `salesRankDrops30/90` is Keepa's
own documented velocity proxy and what CLAUDE.md's 30/60/90-day sales
velocity rule is actually evaluated against (see `keepa_client.py` and
`rules/engine.py`'s docstrings). Added via a normal Alembic migration,
applied and verified the same way as the original schema.

**Matching confidence scoring is multiplicative on brand, not additive.**
An earlier `title_score * 0.8 + brand_score * 0.2` formula let a
same-brand-but-completely-unrelated-product candidate (supplier's "Blue
Widget 10-Pack" vs. the same manufacturer's "Red Garden Hose") clear the
proposal confidence threshold on brand agreement alone. Caught by
`test_matching_engine.py`, not reasoned about in advance. Fixed so brand
only *modulates* title similarity (0.85x-1.0x), never rescues an
unrelated title - see `engine.py`'s `score_candidate` docstring, which
asks whoever touches this next to keep it multiplicative.

**Hard-excluded products (restricted, manufacturer-sells-directly) map to
the `NO_BUY` label**, not a separate "excluded" state - the classification
schema was fixed at step 2 to exactly 5 labels
(Buy/Review/Negotiate/No-buy/High-risk), matching CLAUDE.md's table
literally, and doesn't have a 6th "hard excluded" state. `engine.py`'s
docstring flags this explicitly: a caller building an owner-facing list
should treat a `NO_BUY` whose top `rule_trace` entry has
`result: "hard_exclude"` as "never show as an option," distinct from an
ordinary `NO_BUY` reached on financials alone. This is an interpretation
made to fit the existing schema, not re-litigated at the schema level.

**"Notably strong deal" for a gated-pending item is defined as ROI
clearing 1.5x the normal threshold** (`gated_pending_strong_deal_multiplier`
in `rules/config.py`) - CLAUDE.md asks for this surfacing rule but doesn't
specify a number. Configurable, not hardcoded, and explicitly flagged in
the config as a starting point, not a claimed-precise business figure.

**A rejected `ProductMatch`'s stale confidence score doesn't gate whether
it re-enters the review queue.** Original review-routing logic checked
`match_confidence < threshold` for any non-`CONFIRMED` status, including
`REJECTED` - meaning a rejected match that happened to have a *high*
confidence score before rejection silently fell through without being
flagged for review, even though it has no usable match at all anymore.
Caught by `test_review_routing.py`. Fixed: `REJECTED` always routes to
review, independent of the discarded score - see `routing.py`.

**Per-row error isolation (CLAUDE.md's explicit MVP acceptance criterion)
is verified, not just implemented.** `test_orchestration.py` runs a stub
SP-API client configured to fail for one specific item in a 3-row run and
asserts the run still completes (`status = PARTIAL`, not `FAILED`) with
the other two rows fully processed. This is the test most directly tied
to a named acceptance criterion in CLAUDE.md, so it's called out here
specifically.

**The tool-interface `/runs/{id}/process` endpoint runs synchronously
today**, calling the same `process_run` function `worker.py`'s SQS loop
calls. CLAUDE.md requires background processing for real runs (no ECS
task/service is deployed yet - see 0003), so the endpoint is a stand-in
for local development/testing, not the intended production path. Once
the worker is deployed, the endpoint should probably be removed or
gated, rather than left as a second, request-blocking way to trigger the
same expensive pipeline - flagged here rather than decided unilaterally.

**Export/comparison joins two runs by `supplier_item_number`.** This
reuses the exact key `product_matches` already treats as the persistent
identity of a product from a given supplier (steps 2/7) rather than
inventing a separate comparison key - CLAUDE.md's "support comparing
different lists from the same supplier over time" acceptance criterion is
satisfied by `compare_runs()` in `export.py`, verified with three cases
(changed classification, unchanged, present-in-only-one-run) in
`test_export.py`.

## Verification summary

112 pytest tests, all passing: 82 need no external services at all
(pure logic - parsers' header-detection heuristic, column mapping,
line-item classification, matching score/rank, the entire rule engine's
28 tests covering every branch of CLAUDE.md's rules table, review
routing); 30 run against real local Docker Postgres and/or the real
`adc-prod-supplier-files` S3 bucket (schema round-trips, ingestion's
byte-for-byte S3 preservation, the full API router via `TestClient`,
per-row error isolation). Nothing runs against real SP-API/Keepa - see
the caveat above. `ruff check` clean except two cosmetic style
suggestions in a test fixture, left as-is.
