# Distribuidora Candelaria 2014 LLC — Automation Platform (Phase 1 / MVP)

This file is the canonical project brief. It's loaded automatically at the
start of every session in this repo — read it before doing anything else,
especially in a fresh session that doesn't have prior conversation context.

## Role

Act as senior backend architect and implementation engineer for this
project. Build incrementally, one reviewable piece at a time — don't
generate the entire system in one pass. Explain what you're building and
why before writing code. Flag assumptions explicitly rather than silently
deciding.

## What this system does

An existing Amazon FBA wholesale business needs its supplier-list analysis
process automated. The owner receives product lists from ~10 suppliers
(Excel, PDF, or scanned catalog format — not clean spreadsheets), and
currently reviews each manually to decide what to buy. This system
ingests those lists, matches products to Amazon ASINs, pulls live
pricing/fee/sales data, applies configurable business rules, and
classifies each product — cutting review time from hours to a short list
of decisions the owner approves.

**Core principle: this system never decides — it prepares decisions for
human approval.** No purchase, payment, or other consequential action is
ever executed automatically.

## Scope: Priority 1 only (MVP)

Do not build replenishment, CRM, alerts, or document management — those
are later, separately-scoped phases. This phase is: ingest a supplier
list → normalize it → match to ASINs → pull live data → classify → owner
reviews/approves.

## Architecture (already decided — do not relitigate)

- **Modular monolith**, not microservices. One deployable backend service
  with clearly separated internal modules. Budget and team size (one
  owner, one developer) don't justify microservice overhead yet.
- **Strict separation between orchestration and logic:**
  - **OpenClaw** (separate system, not part of this repo) owns: Telegram
    conversation, receiving file uploads, relaying commands, calling this
    backend as tools, formatting results in plain language.
  - **This backend** owns everything else: file parsing, normalization,
    matching, all financial math, rule evaluation, database writes,
    retries, audit logging. OpenClaw must never contain business logic —
    if you find yourself writing matching or calculation logic that
    OpenClaw would call directly instead of this backend, stop and flag it.
- **Live data, deterministic logic.** "Deterministic" describes the
  calculation code, not the inputs. Prices, fees, seller counts, sales
  estimates are always fetched fresh from SP-API and Keepa on every run —
  never cached as static assumptions, never hardcoded.
- **Database:** PostgreSQL (AWS RDS).
- **Object storage:** AWS S3 — original uploaded files are immutable,
  versioned, never modified in place.
- **Async processing:** list processing (especially 5,000+ rows with live
  API calls per row) must run as background jobs, not in a synchronous
  request. Use a job queue (SQS + worker) with retries and per-row error
  isolation — one bad row must not fail the whole run.
- **Cloud provider:** AWS. Budget ceiling: $500/month total (infra +
  Keepa + any other services). Design with this constraint in mind — flag
  anything that risks blowing the budget.
- **Secrets:** AWS Secrets Manager only. Never in code, config files,
  logs, or committed anywhere. SP-API and Keepa credentials in particular.

## Data reality (confirmed from real supplier files — design against
this, not against clean spreadsheets)

- **No UPC/EAN ever provided by any supplier.** Matching must be
  primarily text/brand/description-based against the Amazon catalog, not
  UPC lookup.
- **Persistent mapping required.** Once a
  `supplier_id + supplier_item_number → ASIN` match is confirmed by the
  owner, save it permanently. Future lists from that supplier with that
  item number should match instantly at high confidence — this is what
  makes review workload shrink over time.
- **Files are often PDF marketing catalogs**, not clean tables —
  multi-column layouts, embedded images, inconsistent headers
  (`UNIT PRICE` vs `SALE` vs `SALE PRICE`), noise columns from prior order
  forms (e.g. leftover `QTY ORDERED`), inconsistent case-quantity units
  (`EA`, `DP`, `CA`, `ST`, `PCS` — `DP` and `ST` need a lookup table, they
  are not standard units).
- **Two special line-item types need distinct handling, not just normal
  parsing:**
  - **Display/bundle SKUs** (kit-level items, e.g. "9 COLOR DISPLAY" at a
    bundle price) — flag for manual review, do not exclude automatically,
    do not attempt normal per-unit ASIN matching.
  - **Tiered promotional pricing blocks** (e.g. "1 pc = $2.90 → $1.45,
    180 pcs = $208.80/DP") — route directly to manual review, do not
    attempt to auto-parse into a standard cost field.
- **PDF and Excel/CSV both need real support** — sample files include
  both. Don't build for one and bolt the other on later.

## Business rules (client-approved — implement as configurable, not
hardcoded)

Rules must be adjustable by a non-technical owner without a code change —
store as structured config (JSON or DB-backed) with sensible defaults
below.

| Factor | Rule |
|---|---|
| ROI | ≥20% default, but soft threshold — near-threshold items classify as "Negotiate" not "No buy" (first-round supplier prices are typically inflated) |
| Margin | ≥10% target, but price-dependent — needs a configurable exception table, not a flat cutoff |
| Sales velocity | ≥50 units/month, with seasonality awareness — needs historical/trend data (30/60/90-day minimum) not a single snapshot |
| Restricted products | Hard exclude — never shown as an option |
| Gated products (require approval) | Two sub-states: (a) already approved for this seller account → treat as normal; (b) not yet approved → don't discard, surface only if ROI/margin is notably strong, flag clearly that owner must submit invoice to Amazon and await approval before it's purchasable |
| Manufacturer sells directly on the listing | Hard exclude — cannot compete |
| Amazon has the Buy Box | Not excluded — flagged prominently as higher risk/difficulty |
| Seller count | Risk-scoring factor, not a hard cutoff |
| Display/bundle SKUs | Flag for manual review, not auto-excluded |
| Tiered promo pricing blocks | Route to manual review, not auto-parsed |
| Any genuinely ambiguous case | Manual review — never guess |

Classification output per product: **Buy / Review / Negotiate / No-buy /
High-risk**, always with the underlying reasoning/rule trace shown — the
owner must be able to see *why*, not just the label.

## MVP acceptance criteria (from client brief)

- Process ≥5,000 rows per list without manual intervention per row.
- Show processing progress, successful rows, errors, unprocessed rows,
  match confidence.
- Allow the owner to confirm/correct column mapping before processing.
- Save every analysis run; support comparing different lists from the
  same supplier over time.
- Rules changeable without touching code.
- Export filtered results; preserve the original source file untouched.

## Build order

1. ✅ Repo structure, environment config, AWS resource provisioning (RDS,
   S3, Secrets Manager, SQS) — infra as code.
2. ✅ Database schema: suppliers, list runs, raw line items, product
   matches (confidence + status), Amazon data snapshots (per run), 
   classification results (rule trace), manual review queue.
3. ✅ File ingestion: upload handling, original file preservation to S3,
   PDF + Excel/CSV parsing.
4. ✅ Column/field normalization with confidence-scored proposed mapping,
   owner confirmation step.
5. ✅ SP-API integration (catalog search for text matching, pricing/fees,
   restrictions/gated status). **Code-complete, unverified against live
   SP-API — see status notes below.**
6. ✅ Keepa integration (price history, sales rank). **Code-complete,
   unverified against live Keepa — see status notes below.**
7. ✅ Matching engine: text/brand matching against Amazon catalog,
   confidence scoring, persistent mapping table.
8. ✅ Rule engine: configurable rules from the table above, classification
   output with reasoning trace.
9. ✅ Manual review queue logic (low-confidence matches, promo blocks,
   bundle SKUs, gated-pending items, any ambiguous case).
10. ✅ Tool-callable interface for OpenClaw to trigger runs, check status,
    relay approvals.
11. ✅ Export and run-history/comparison features.
12. ✅ Tests for matching logic and rule engine specifically.

## Working style

- One piece at a time, confirm it works before moving to the next.
- Flag any point where a design decision here would be hard to reverse
  later.
- If SP-API or Keepa's actual capabilities differ from what's assumed
  above, say so rather than working around it silently.
- Never hardcode credentials, ever, anywhere — always AWS Secrets Manager.
- This system's outputs directly drive real purchase decisions —
  correctness and explainability matter more than speed of delivery.

---

## Current status (updated 2026-08-07)

**Steps 3-12 (the entire remaining MVP pipeline) are built, at the user's
explicit direction to implement the full remaining build order in one
pass** — a deliberate, acknowledged departure from this file's own "one
piece at a time" working style above, not an oversight. 112 pytest tests
pass (82 pure-logic, no external services needed; 30 against real local
Postgres and/or the real `adc-prod-supplier-files` S3 bucket).

**The one caveat that matters most:** SP-API and Keepa clients (steps
5-6) are code-complete against each API's documented contract and tested
against hand-written mocks, but have **never made a real network call** —
only placeholder credentials exist in Secrets Manager. Everything
downstream that depends on live pricing/fees/restrictions/sales data
(matching, the rule engine's financial math, the full run-processing
pipeline) is exercised via stub clients standing in for the real thing.
The first real SP-API/Keepa call, whenever real credentials are entered,
is the actual verification of that half of the system — not something to
assume from tests passing. Full reasoning, plus several real bugs found
and fixed along the way (a noise-column false-positive in column mapping,
a same-brand-false-positive in match scoring, a stale-confidence gap in
review routing), is in
`docs/decisions/0004-build-steps-3-12.md`.

**Infra: fully applied as of 2026-08-07.** Every resource `infra/`
defines now exists for real in AWS (account `617464676572`, `us-east-1`)
— VPC/networking, RDS, S3, both Secrets Manager placeholder secrets,
SQS+DLQ, ECR, the ECS cluster, and both IAM roles with policies.
`terraform plan` reports "No changes" against 42 tracked resources.
Getting there took two more rounds of IAM permission gaps (each found via
a real apply attempt, not guessed) plus one genuine account-level
discovery: **this AWS account is on some kind of free-tier/restricted
plan** that rejected RDS's backup retention period outright — worked
around by setting it to `0` (no automated backups) for now, explicitly
flagged in `modules/rds/main.tf` as needing revisiting (same category as
the already-flagged `deletion_protection`) before this holds real data
with no backup recovery point otherwise. Full narrative in
`docs/decisions/0003-infra-apply-findings.md`.

**Next:** populate the real SP-API/Keepa secrets (`aws secretsmanager
put-secret-value`, see `infra/README.md`) and run the pipeline against
live data for the first time — this is the actual test that steps 5-8
work, not just that their mocks pass. Also worth resolving before real
data exists: raise the backup-retention restriction above (or otherwise
address whatever plan limit caused it), and flip `deletion_protection`
once this holds anything real. An ECS task definition/service is still
the one piece of `infra/` not built — add it once there's a container
image worth deploying.

---

## Status as of step 2 (2026-08-04) — earlier session, kept for history

**Step 2 is complete and locally verified:**
- SQLAlchemy 2.0 models + Alembic migrations for all 9 core tables
  (`suppliers`, `list_runs`, `raw_line_items`, `unit_lookup`,
  `product_matches`, `amazon_data_snapshots`, `business_rules_configs`,
  `classification_results`, `manual_review_queue`), split across the
  module that owns each concept (`db/core_models.py` for the two shared
  entities, `modules/{ingestion,matching,amazon,rules,review}/models.py`
  for the rest).
- Verified against local Docker Postgres (port 5433 — 5432 is occupied by
  an unrelated project's container on this machine): full
  `upgrade → downgrade → upgrade` cycle actually run (not just asserted),
  plus 4 pytest tests exercising relationships and constraints
  (persistent supplier+item-number match uniqueness, required rule-trace
  audit field) — `pytest` passes 4/4.
- Found and fixed a real bug along the way: Alembic's autogenerated
  `downgrade()` doesn't drop Postgres native ENUM types, only the tables
  using them — caused a `DuplicateObject` error on re-upgrade after
  downgrade. Fixed by hand in the initial migration; noted for future
  migrations that add new enum columns.
- Local dev needs no AWS credentials: `DATABASE_URL` env var (see
  `.env.example`, `backend/docker-compose.yml`) bypasses Secrets Manager
  entirely for local Postgres. Must stay unset in any real environment.
- Full reasoning in `docs/decisions/0002-database-schema-decisions.md`.
- **Not yet applied against real RDS** — infra/ still hasn't been applied
  to AWS (see AWS CLI status below). Re-verify against RDS once it has.

**Step 1 is complete and locally verified:**
- `backend/` — Python/FastAPI skeleton, `/health` endpoint, config module
  that reads AWS Secrets Manager at runtime (no secret values anywhere in
  code/config). `pytest` passes (1/1) — verified by actually installing
  deps into a venv and running the suite.
- `infra/` — Terraform for VPC, RDS, S3, Secrets Manager (placeholder
  secrets), SQS+DLQ, ECR, ECS cluster + IAM roles. **No ECS task
  definition/service yet** — deferred until there's a container image
  worth deploying (comes with/after step 3).
- `terraform validate` passes for both `infra/` and `infra/bootstrap/`
  (Terraform installed via winget; ran `init -backend=false` + `validate`
  — full syntax/type check, not a plan against real AWS).
- **Not yet applied to AWS.** No AWS credentials are configured on this
  machine. Nothing has been provisioned in the actual AWS account.
- Full reasoning for the calls made in step 1 (no NAT gateway, RDS
  deletion-protection off for now, single environment, AWS-managed RDS
  master password, SQS message-per-run not per-row) is in
  `docs/decisions/0001-initial-architecture-decisions.md`. Two flagged
  items worth re-checking before go-live: NAT-less networking (fine until
  an ALB/inbound path is needed) and RDS deletion protection (must flip
  to `true` before real data exists).

**AWS CLI install has been stuck on this machine across several attempts**
(hung MSI/UAC issue when attempted non-interactively, then a leftover
Windows Installer service lock causing "another installation in
progress" on the user's own retry, then `Restart-Service msiserver` also
failed). Current recommendation is a full reboot, then
`winget install -e --id Amazon.AWSCLI` in a fresh terminal, then
`aws configure`/`aws configure sso` — **run in a terminal outside of any
AI chat session** so keys never land in a transcript. Status of that
reboot/retry is not confirmed as of this writing — check with the user
before assuming it's resolved.

**Decision: not blocking on AWS credentials.** Step 2 (database schema)
doesn't need AWS — it's SQLAlchemy models + Alembic migrations, verified
against a local Postgres via Docker (already available on this machine;
note host port 5432 is occupied by an unrelated project's container, so
use a different port for this project's local Postgres). Build and verify
step 2 locally now; re-verify against real RDS once `infra/` is applied.
Same reasoning extends loosely to steps 3-4 (ingestion/normalization are
local parsing logic). AWS becomes unavoidable at `terraform apply` and
anything touching real S3/SQS/Secrets Manager or SP-API/Keepa credential
storage.

**Next: step 3, file ingestion** — start here in a new session regardless
of AWS CLI/credential status, using local Docker Postgres.

See `README.md` for repo layout and local dev instructions.
