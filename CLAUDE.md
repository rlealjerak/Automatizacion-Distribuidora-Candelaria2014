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

## Current status (updated 2026-08-20, evening — Goal 1 of `NEXT_PHASE_PLAN.md`)

**Goal 1 (SQS worker as an independent ECS service) is done and fully
live-verified — both of `NEXT_PHASE_PLAN.md`'s two goals were sequential
by design (Goal 2/TLS blocked on external domain registration), so only
Goal 1 was in scope this session.**

`worker.py` is now deployed as its own ECS Fargate service
(`adc-prod-worker`, no load balancer — it takes no inbound traffic),
sharing the API's image with `command` overridden at the task-definition
level to skip `entrypoint.sh` and run the worker's long-poll loop
directly (`infra/modules/ecs_cluster/main.tf`). `POST
/runs/{run_id}/process` no longer runs the pipeline inline — it now
validates the run (404/400 fast-fail before touching SQS) and enqueues
`{"run_id"}` to `adc-prod-list-processing`, returning `202` immediately
(`modules/tools/router.py`). Built and pushed as image `v3`, then `v4`
(see next finding) — `terraform apply` created the worker service and
rolled the API onto the new image; both confirmed steady, `terraform
plan` reports **no changes**.

**Live-verified end-to-end, for real, against production:** uploaded a
synthetic 1-row list through the real API, confirmed mapping, called
`/process` (**202 in ~0.16s**, not a multi-second synchronous SP-API/Keepa
round trip), watched the worker's real CloudWatch logs make real SP-API
calls (catalog search, pricing, fees, restrictions) and a real Keepa
call, land a real ASIN match, and classify `no_buy` on real negative ROI
— run status flipped to `completed` within seconds. Separately verified
the failure path for real: temporarily dropped the queue's
`VisibilityTimeout` to 10s (restored to 1800s immediately after — no
Terraform drift), sent a message with a nonexistent `run_id`, and
confirmed via `ApproximateReceiveCount: 4` on the DLQ message that SQS's
native redrive (`maxReceiveCount=3`) moved it to
`adc-prod-list-processing-dlq` on its own — nothing worker.py needed to
implement itself, exactly per its own design intent.

**Real bug found and fixed along the way, not hypothetical:** `httpx`
(used by both `sp_api_client.py` and `keepa_client.py`) logs the full
request URL at `INFO` by default, and Keepa's API takes its key as a URL
query param (`?key=...`) rather than a header. Neither `main.py` nor
`worker.py` suppressed httpx's logger, so **the real Keepa API key has
been written into CloudWatch logs in plaintext on every live Keepa call
since 2026-08-15** — this session is just the first time anyone read
those logs closely enough to notice. Fixed in both files
(`logging.getLogger("httpx").setLevel(logging.WARNING)`), 122/122 tests
still pass, confirmed live post-fix (`v4`): the same live Keepa call now
produces zero request-line log output. **The exposed key itself has not
been rotated** — the user made an explicit, informed call to leave
rotation for their own schedule rather than do it in this session, aware
that the old value is sitting in both CloudWatch (30-day retention) and
this session's transcript. Worth revisiting before this matters more
(real supplier data, higher Keepa call volume).

**New, smaller IAM gap found, not fixed:** `claude-code` can
`logs:DescribeLogStreams`/`GetLogEvents` but not `logs:FilterLogEvents`
or `logs:DeleteLogStream` — blocked a first cleanup attempt on the
leaked-key log stream (worked around by using `DescribeLogStreams` +
`GetLogEvents` directly instead, and by favoring key rotation over log
scrubbing as the actual fix). Same category as the earlier six IAM
rounds in `infra/claude-code-iam-policy.json`, just not urgent enough to
fix unprompted this session.

**Leftover in prod, harmless but worth knowing about:** a supplier row
named "Goal1 Async Verification Supplier" (`code: goal1-async-verify`)
and two synthetic 1-row test runs against it, created during live
verification. No delete endpoint exists yet for either; left in place
rather than reached into RDS directly outside the app's own write paths.

**Goal 2 (TLS/HTTPS) untouched this session** — still correctly blocked
on domain registration clearing in Route53, per the plan's own
sequencing. Check that before starting it.

---

## Current status (updated 2026-08-20)

**RDS backup retention closed — one of the two long-flagged infra gaps.**
`backup_retention_period` had been `0` since the original apply
(2026-08-07) because this AWS account's plan rejected any nonzero value
outright (`FreeTierRestrictionError`). The user upgraded the account's
plan; nonzero values are now accepted. Changed the default in
`infra/variables.tf` (`db_backup_retention_period`) from `0` to `7` and
confirmed live: `aws rds describe-db-instances` shows
`BackupRetentionPeriod: 7` on `adc-prod-db`, `terraform plan` reports "No
changes" against the updated config, and `terraform apply` (a no-op
against real infra) resynced the local state file, which had gone stale
— the retention change was applied directly against AWS, not through
Terraform, so `terraform state show` still read the old `0` until
refreshed.

**Still open:** `deletion_protection` (`infra/variables.tf` /
`modules/rds/main.tf`) — same category, still `false`, not addressed by
this change. Revisit before this holds real supplier/pricing data no one
has a recovery path for otherwise.

---

## Current status (updated 2026-08-16)

**This system is deployed and reachable for the first time ever.**
`http://<alb_dns_name>/health` returns 200 from a real, running ECS
Fargate task behind a real ALB — the one piece of `infra/` flagged as
undone since the original apply is now built: `modules/alb` (new) +
`modules/ecs_cluster`'s task definition/service (previously deferred).
Full narrative of what it took: six real IAM permission rounds (ELB
actions, two service-linked-role grants — one needed an explicit
`aws iam create-service-linked-role`, ECS's own auto-create didn't
reliably trigger — plus a few narrowly-scoped EC2/ECS gaps), all fixed
in `infra/claude-code-iam-policy.json`; two real bugs from actually
building and deploying the container for the first time (wrong CPU
architecture - built on this arm64 Mac without `--platform linux/amd64`,
Fargate couldn't pull it; and an alembic `configparser` interpolation
crash on the real RDS password's percent-encoded characters, never
caught locally since the dev password has none of those). Both fixed,
both verified against the real deployment.

**Before any of that, a security gap had to close first:** the API had
*zero authentication* anywhere. Added a shared `X-Api-Key` header,
enforced on every route except `/health` (`backend/src/adc_backend/
modules/auth.py`), backed by a new `adc/prod/api-key` secret. Verified
live: 401 without the header, 200 with the real key.

**Also verified live, closing a gap flagged earlier the same day:**
`seed_default_rules_config()` had never run against real RDS — it now
runs automatically on every container start (`entrypoint.sh`, alongside
`alembic upgrade head`, both idempotent), confirmed via real CloudWatch
logs on the first real deploy: migrations ran, config seeded, classify
is no longer a silent no-op in production.

**Known, deliberately-flagged-not-fixed gap: no TLS.** The ALB is
HTTP-only — no domain name or ACM certificate exists yet, so `X-Api-Key`
currently travels in plaintext over the internet. Same category as the
already-flagged RDS `backup_retention_period`/`deletion_protection`
gaps — needs a real decision (domain + cert) before this carries
anything beyond dev/test traffic, not something to silently accept
long-term.

**Also flagged, not blocking:** ECR has immutable tags (found live — a
`:latest` re-push failed); `container_image_tag` now defaults to an
explicit version (`v2`) bumped by hand each deploy. A real CI/deploy
pipeline should use git-SHA tags instead.

**Next:** decide on a domain + ACM cert for HTTPS before any real
traffic; give OpenClaw the ALB DNS name + the real API key (out of band,
never in a transcript) so it can actually call this backend; work out a
deploy story better than "build/push/bump-a-tf-var by hand" (this
session's process, fine for a first deploy, not for ongoing iteration).

---

## Current status (updated 2026-08-15)

**Dev machine changed: this project is now worked on from a Mac**
(previously Windows — see the AWS-CLI-install saga in the 2026-08-04
status block below, which no longer applies). A fresh clone onto this
machine needed re-doing all local environment setup from scratch;
verified working now:
- **AWS CLI is authenticated correctly** as IAM user `claude-code` in
  account `617464676572` (the account `infra/backend.tf`'s state bucket
  and all applied resources actually live in) — a fresh `aws configure`
  on this machine initially pointed at a *different*, unrelated AWS
  account (`570634575880`); caught before anything ran against it, then
  the user reconfigured credentials for the right account.
- **Terraform installed** at `~/.local/bin/terraform` (v1.15.8 — `brew
  install terraform` failed here on outdated Command Line Tools, so
  installed from the official release zip instead; `~/.zshrc` already
  puts `~/.local/bin` on `PATH`). `terraform init` + `terraform plan`
  against the real `617464676572` state: **"No changes."** — confirms
  the infra apply from 2026-08-07 is still intact and unchanged.
- **Backend Python venv rebuilt** (`backend/.venv`, same dependency list
  as before). Local Postgres via `docker compose up -d` (port 5433),
  `alembic upgrade head` clean, **all 112 pytest tests pass** — but only
  once `DATABASE_URL` and `S3_BUCKET_NAME` are `export`ed in the shell,
  not just present in `backend/.env`; pytest fixtures read the process
  environment directly, `.env` is only loaded by the app at runtime via
  `pydantic-settings`. This was silently skipping 30 of 112 tests before
  that was caught — README's local-dev section fixed to call this out,
  plus its `.venv/Scripts/...` (Windows-only) paths corrected to
  `.venv/bin/...` for Mac/Linux, with a Windows note kept alongside.
- **Secrets Manager checked (metadata only, not values):**
  `adc/prod/sp-api-credentials` was last changed 2026-08-10 — matches
  the real-LWA-credentials commit (`6951cb6`) and confirms that update
  actually landed in the real account. **`adc/prod/keepa-api-key` has
  never been changed since its Terraform-placeholder creation on
  2026-08-06** — Keepa is still fully unverified against live data, and
  putting a real key there is the next concrete blocking step (see
  `infra/README.md` for the `put-secret-value` command).
- **RDS** (`adc-prod-db`): available, `backup_retention_period = 0`,
  `deletion_protection = false` — both still exactly as flagged in
  `docs/decisions/0003-infra-apply-findings.md`; unchanged, still open.
- **New IAM gap found:** the `claude-code` user can't call
  `ecs:ListTaskDefinitions` / `ecs:ListClusters` — irrelevant today since
  no ECS task definition exists yet (still the one undone piece of
  `infra/`), but will need a policy update once that's built.
- 16 pre-existing `ruff` lint findings in `backend/` — not introduced by
  this session, not fixed, just noted.

**Update same day: real Keepa API key added and live-verified.** A real
key is now in `adc/prod/keepa-api-key` (changed 2026-08-15). A one-off
smoke script (not part of the test suite) called `KeepaClient.get_product()`
against several real ASINs and got real data back (e.g. `0439023483` →
"The Hunger Games", rank 2577, price $11.94, `salesRankDrops30=46`).
That live check caught a real bug, now fixed: Keepa's `-1` "no data"
sentinel was normalized for the array-based stats fields but not for the
scalar `salesRankDrops30`/`90` fields, so an untracked ASIN's `-1` was
flowing through as a literal very-low-velocity number instead of `None`
(no data → should route to manual review). Fixed in `keepa_client.py`
(`_none_if_negative`); 112/112 tests still pass. Module docstring updated
to reflect live-verified status.

**Update same day: SP-API fully live-verified, real bugs fixed, and a
full real end-to-end pipeline run completed successfully.**

`get_pricing`/`get_fees_estimate`/`get_listing_restrictions` all called
live for the first time, against real ASINs. Two real, load-bearing bugs
found and fixed (both now covered by regression tests, 116/116 passing):
1. `get_fees_estimate` never sent `IsAmazonFulfilled=True` — SP-API
   silently estimated merchant-fulfilled fees instead of FBA fees, so
   `fba_fee` came back `None` on every real ASIN despite this being an
   FBA-only business.
2. `get_listing_restrictions` didn't scope to a condition type and didn't
   recognize the reason code SP-API actually returns (`NOT_ELIGIBLE`) —
   a genuinely restricted real ASIN came back as neither restricted nor
   gated. Fixed by scoping to `conditionType=new_new`.

A third finding wasn't a bug to silently fix — `NOT_ELIGIBLE` turned out
to mean two different real things (permanent restriction vs. a clearable
brand-authorization gate) under the *identical* reason code on the same
ASIN. Per explicit user decision after seeing the live evidence, this now
routes to manual review (new `ambiguous_restriction` flag threaded
through `RestrictionsResult` → `AmazonDataSnapshot` → rule engine → review
queue, migration `e64eb0eeb98c`) rather than guessing either direction.

**Full real end-to-end pipeline run**, driven through the actual
OpenClaw-facing HTTP API (not internal function calls) against real
Postgres, real S3, real SP-API, and real Keepa — a synthetic-but-real
5-row supplier list covering every special case CLAUDE.md calls out:
a standard item with an ambiguous restriction (correctly landed in the
review queue, full live reasoning trace confirmed in the DB), a standard
item with clean live financials (correctly classified `NO_BUY` on real
negative ROI), a display/bundle SKU, a tiered-promo-pricing block, and a
row with no supplier item number. All five landed exactly where CLAUDE.md
says they should. Column mapping, ASIN matching, live pricing/fees/
restrictions/velocity, rule engine, and review-queue routing all worked
together for real, for the first time.

**One real deployment gap found and worth acting on before any real
supplier list is run against production:** `seed_default_rules_config()`
(`rules/config.py`) has never been run against real RDS — only ever
against local dev Postgres in earlier test sessions. Without it,
classification silently no-ops on every row (found live during this run;
harmless in that it fails safe into "needs review," but it means zero
`Buy`/`No-buy`/etc. output until someone runs this once). Test file used
for the live run was cleaned up from the real `adc-prod-supplier-files`
S3 bucket afterward (soft-deleted — versioning is on, per infra).

**Next:** run `seed_default_rules_config()` against real RDS before the
first real supplier list. After that, nothing in the MVP pipeline is
still unverified against live data — the remaining build-order item is
the ECS task definition/service (never built) to actually deploy this,
plus the RDS `backup_retention_period`/`deletion_protection` flags still
open from the infra-apply findings.

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
