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
    main.py             FastAPI app - becomes the OpenClaw-facing tool interface
    worker.py           SQS worker entrypoint (background list processing)
    db/
      base.py            Declarative Base, engine, session factory
      core_models.py      Supplier, ListRun (shared across every module)
      models.py            Aggregator - imports every module's models for Alembic/tests
    modules/
      ingestion/         File upload, S3 preservation, PDF + Excel/CSV parsing      (step 3)
        models.py          RawLineItem, UnitLookup                                  (step 2 ✅)
      normalization/     Column mapping, unit lookup table, owner confirmation       (step 4)
      amazon/            SP-API + Keepa clients                                      (steps 5-6)
        models.py          AmazonDataSnapshot                                       (step 2 ✅)
      matching/          Text/brand ASIN matching, persistent mapping table          (step 7)
        models.py          ProductMatch                                             (step 2 ✅)
      rules/             Rule engine, financial math, classification                 (step 8)
        models.py          BusinessRulesConfig, ClassificationResult                 (step 2 ✅)
      review/            Manual review queue routing                                 (step 9)
        models.py          ManualReviewQueue                                        (step 2 ✅)
      tools/             OpenClaw-facing boundary                                    (step 10)
  alembic/            Migrations (env.py wired to the same DB config as the app)
  docker-compose.yml  Local-dev-only Postgres (port 5433) for schema/migration work
  tests/

infra/             Terraform (AWS: RDS, S3, Secrets Manager, SQS, ECR, ECS, VPC)
  bootstrap/         One-time remote state backend setup - run this first
  modules/

docs/
  decisions/         Architecture decision records
```

## Status

Build-order steps 1-2 are in place:
- **Step 1** — Python app skeleton with a working health check + test,
  and Terraform for all MVP AWS resources except the ECS service itself
  (added once there's a container image worth deploying). See
  `docs/decisions/0001-initial-architecture-decisions.md`, including two
  items flagged as worth revisiting before go-live (RDS deletion
  protection, NAT-less networking).
- **Step 2** — SQLAlchemy models + Alembic migrations for all 9 core
  tables, verified end-to-end against local Docker Postgres (upgrade →
  downgrade → upgrade cycle, plus pytest coverage of the persistent
  match-mapping and rule-trace constraints). See
  `docs/decisions/0002-database-schema-decisions.md`.

Nothing here talks to SP-API or Keepa yet, and nothing has been applied
against real AWS/RDS — that's step 3 onward / once AWS credentials are
configured.

## Local development

```bash
cd backend
python -m venv .venv
./.venv/Scripts/pip install fastapi "uvicorn[standard]" pydantic-settings boto3 sqlalchemy alembic psycopg2-binary pytest httpx
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
