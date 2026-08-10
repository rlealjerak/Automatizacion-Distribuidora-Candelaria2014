# Distribuidora Candelaria 2014 LLC — Automation Platform

Priority 1 / MVP: ingest a supplier product list → normalize it → match
products to Amazon ASINs → pull live pricing/fee/sales data → apply
configurable business rules → classify each product (Buy / Review /
Negotiate / No-buy / High-risk) → owner reviews and approves.

**This system never decides — it prepares decisions for human approval.**
No purchase, payment, or other consequential action is ever executed
automatically.

## Architecture

Modular monolith (one deployable backend, clearly separated internal
modules) — not microservices. Strict separation: **OpenClaw** (a separate
system, not in this repo) owns Telegram conversation, file uploads, and
formatting; it calls into **this backend** as a tool. This backend owns
everything else — parsing, matching, financial math, rule evaluation, DB
writes, retries, audit logging. Business logic never lives in OpenClaw.

All product/pricing/sales data is fetched live from SP-API and Keepa on
every run. "Deterministic" describes the calculation code, not the
inputs — nothing here is a cached/static assumption.

## Repo layout

```
backend/          Python backend (modular monolith)
  src/adc_backend/
    config.py          Settings + Secrets Manager access (no hardcoded secrets, ever)
    main.py             FastAPI app - the OpenClaw-facing tool interface (step 10)
    worker.py           SQS worker entrypoint - real, runnable against the live queue (step 10)
    db/
      base.py            Declarative Base, engine, session factory
      core_models.py      Supplier, ListRun (shared across every module)
      models.py            Aggregator - imports every module's models for Alembic/tests
    modules/
      ingestion/         Upload handling, S3 preservation, PDF/Excel/CSV parsing        (step 3)
        service.py, parsers.py, storage.py, models.py
      normalization/     Column mapping (confidence-scored), unit lookup, item typing   (step 4)
        column_mapping.py, classify_line_item.py, unit_lookup.py, service.py
      amazon/            SP-API + Keepa clients - code-complete, unverified live         (steps 5-6)
        sp_api_client.py, keepa_client.py, models.py
      matching/          Text/brand ASIN matching, persistent mapping table              (step 7)
        engine.py, models.py
      rules/             Rule engine: ROI/margin/velocity/risk, full reasoning trace      (step 8)
        engine.py (pure), config.py (defaults), service.py, models.py
      review/            Manual review queue routing                                     (step 9)
        routing.py (pure), service.py, models.py
      tools/             OpenClaw-facing boundary: router, schemas, orchestration, export (steps 10-11)
        router.py, schemas.py, orchestration.py, export.py
  alembic/            Migrations (env.py wired to the same DB config as the app)
  docker-compose.yml  Local-dev-only Postgres (port 5433) for schema/migration work
  tests/              112 tests - see "Status" below

infra/             Terraform (AWS: RDS, S3, Secrets Manager, SQS, ECR, ECS, VPC)
  bootstrap/         One-time remote state backend setup - run this first
  modules/

docs/
  decisions/         Architecture decision records
```

## Status

Build-order steps 1-12 (the full MVP pipeline) are built. Steps 1-2 are
verified against local Postgres; see `docs/decisions/0001-...md` and
`0002-...md`. Steps 3-12 were built in one pass at the user's explicit
direction (a deliberate departure from CLAUDE.md's normal incremental
style) — full reasoning, including several real bugs found and fixed via
the test suite, in `docs/decisions/0004-build-steps-3-12.md`.

**The caveat that matters most:** SP-API and Keepa clients (steps 5-6)
have never made a real network call - only placeholder credentials exist.
Everything downstream (matching, financial math, the full pipeline) is
tested against mocks shaped like each API's documented contract, not
recorded real responses. See `modules/amazon/sp_api_client.py` and
`keepa_client.py`'s module docstrings.

`infra/` still needs to be applied against real AWS - nothing here has
been provisioned yet at this point.

112 pytest tests pass (`./.venv/Scripts/pytest -v`) - 82 need no external
services, 30 run against real local Postgres and/or the real
`adc-prod-supplier-files` S3 bucket (skip cleanly if `DATABASE_URL`/
`S3_BUCKET_NAME` aren't set).

## Local development

```bash
cd backend
python -m venv .venv
./.venv/Scripts/pip install fastapi "uvicorn[standard]" pydantic-settings boto3 sqlalchemy alembic psycopg2-binary pdfplumber openpyxl rapidfuzz python-multipart pytest httpx reportlab ruff
cp .env.example .env   # DATABASE_URL in here points at the docker-compose Postgres below
docker compose up -d   # local Postgres on port 5433 (5432 is often taken by other projects)
./.venv/Scripts/python -m alembic upgrade head
./.venv/Scripts/pytest -v
```

(A `pyproject.toml` for Poetry is in place too, for once Poetry is
installed locally / in CI — the venv+pip steps above are just what was
available to verify these increments.)

## Infra

See `infra/README.md`.
