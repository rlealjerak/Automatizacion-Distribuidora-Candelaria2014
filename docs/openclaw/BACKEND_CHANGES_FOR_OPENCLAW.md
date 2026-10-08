# Backend Changes Required for OpenClaw Integration

**Purpose:** This document lists every change needed in the `adc_backend` codebase to support OpenClaw as the Telegram approval layer. It's a consolidation of decisions already locked in `OPENCLAW_APPROVAL_STATE_MACHINE_SPEC.md` and `OPENCLAW_TECHNICAL_SPEC.md` — those documents contain the full reasoning; this one is the actionable checklist derived from them, organized by what part of the codebase each change touches.

**Scope boundary:** None of this touches the existing Priority-1 pipeline (`ingestion`, `matching`, `amazon`, `rules` modules) except for one new integration point in item 4. `classification_results` and its related tables are read-only from OpenClaw's perspective — referenced, never modified.

---

## 1. New database table: `approval_queue`

**New file:** `backend/src/adc_backend/modules/approvals/models.py` (new module — `approvals` doesn't exist yet, sits alongside `rules`, `ingestion`, `matching`, `amazon`)

**Why a new module and not adding to `rules`:** `approval_queue` is workflow/state-tracking data (where is this item in the Telegram back-and-forth) — conceptually distinct from `classification_results`, which is the business-rules *output*. Keeping them separate avoids conflating "what the analysis concluded" with "what's happening in the approval conversation about it."

**Schema:**

| Column | Type | Notes |
|---|---|---|
| `id` | UUID, PK | Match existing convention (`classification_results.id` style — `UUID(as_uuid=True)`, default `uuid.uuid4()`) |
| `analysis_result_ref` | UUID, FK → `classification_results.id`, NOT NULL | The only link to the analysis schema. See Section 4 for how rows here get created. |
| `type` | string/enum | `purchase_recommendation` for now (MVP); extensible for Phase 2+ (replenishment decisions, etc.) |
| `status` | Postgres native ENUM | Values: `queued`, `pending`, `reminder_1`...`reminder_6`, `holding`, `approved`, `denied`. Follow the same native-enum pattern as `classification_results.classification` (`sa.Enum(...)`), including the Alembic hand-edit needed for enum quirks (see Section 2). |
| `queue_position` | integer, nullable | Null when this is the active item; integer order among `queued` items |
| `summary` | text, NOT NULL | Human-readable string OpenClaw sends verbatim to Telegram — backend generates this, OpenClaw does not reformat it |
| `summary_payload` | JSONB, NOT NULL | Denormalized snapshot of just the fields needed to render `summary` (ASIN, ROI, margin, cost, supplier) — not a full copy of the analysis row, just what's needed to redisplay without a live join |
| `created_at` | timestamp, server_default `now()` | |
| `pending_started_at` | timestamp, nullable | Set when item becomes active; resets on state change back to a fresh pending cycle |
| `last_reminder_at` | timestamp, nullable | |
| `reminder_count` | integer, default 0 | |
| `resolved_at` | timestamp, nullable | |
| `resolution` | enum, nullable | `approved` / `denied` |
| `resolution_note` | text, nullable | Captured from the client's free-text reply if any |

**Why each of the timing fields exists:** `pending_started_at` / `last_reminder_at` / `reminder_count` are what the reminder-scheduling job (Section 5) reads to decide "is this item due for its next reminder" — per the locked state machine: 5 min initial wait, then a reminder every 10 min, up to 6 reminders, then indefinite `holding` with no more messages.

---

## 2. Migration

**New file:** `backend/alembic/versions/<hash>_add_approval_queue.py`

Follow the exact pattern used in the initial migration for `classification_results` (`backend/alembic/versions/4028e416c123_initial_schema.py:165-182`) — `op.create_table` with explicit `sa.Column`, `sa.ForeignKeyConstraint`, `sa.PrimaryKeyConstraint`.

**Important — do not skip this step:** per `docs/decisions/0002-database-schema-decisions.md` (already in the codebase), `alembic revision --autogenerate` misses new enum values on existing Postgres enum types and `downgrade()` doesn't drop enum types automatically. The `status` and `resolution` enum columns in this table need the same hand-edit treatment already documented for `amazon_data_snapshots`' prior migrations. Generate the migration, then manually verify the enum creation/drop statements before running `alembic upgrade head`.

No Terraform changes needed for this — `entrypoint.sh` already runs `alembic upgrade head` on every container start, so this table gets created automatically on next deploy.

---

## 3. New MCP-facing endpoint (biggest piece of new code)

**Why this can't just be a normal REST endpoint:** OpenClaw connects to external tools via the Model Context Protocol (MCP), specifically the `streamable-http` transport — not plain REST. This is a hard requirement from OpenClaw's side, confirmed against its official config schema; there's no way to point OpenClaw at ordinary `GET`/`POST` JSON endpoints.

**New dependency:** add the official MCP Python SDK (`mcp` package) to `pyproject.toml` / `requirements.txt`.

**New module:** `backend/src/adc_backend/modules/mcp_server/` — wraps the approval-queue logic as MCP tools. Recommend mounting this as a sub-route on the existing FastAPI app (e.g. `/mcp`) rather than standing up a second deployable service — simpler infra, and there's no reason for this to be a separate process.

**Three tools to implement**, exact schemas already specified in `OPENCLAW_TECHNICAL_SPEC.md` Section 4 (copy directly from there — field names, types, and example payloads are already finalized):

- **`list_queue`** — no input; returns the active item plus all queued items in order. Reads `approval_queue`, joins to `classification_results` (and its related tables: `raw_line_items`, `amazon_data_snapshots`, `product_matches`, `suppliers`) only if `summary_payload` needs to be freshly regenerated — normally it just returns the stored `summary`/`summary_payload` directly without a live join.
- **`approve_decision`** — input: `approval_id`, `resolution` (`approved`/`denied`), optional `note`. **Must reject calls where `approval_id` doesn't match the current active item** — this is the server-side enforcement of the one-active-item rule; never trust the caller's claim about what's active. **Must be idempotent** — a duplicate call with the same `approval_id` + `resolution` returns the same success result rather than erroring, because OpenClaw's retry behavior on timeout is unconfirmed (see `OPENCLAW_TECHNICAL_SPEC.md` Section 6, item 1) and this is the safe mitigation regardless of the answer.
- **`reorder_queue`** — input: `approval_id`. Moves a queued item to the front (next up after the current active item resolves). No validation needed beyond "item exists and is in `queued` state" — the *decision* to only call this on explicit client instruction is enforced by OpenClaw's own agent instructions (Section 4 below), not by this endpoint.

**Auth:** new middleware validating a static bearer token (`CANDELARIA_BACKEND_TOKEN`, provisioned in Secrets Manager) on every request to `/mcp/*`. This is a new, separate credential from your SP-API/LWA credentials — don't reuse those.

---

## 4. Integration point with the existing rules/classification pipeline

**This is the part that isn't just "add new isolated code" — something has to actually populate `approval_queue`.**

Per the locked state machine, rows get created in `approval_queue` in two situations:

1. **From the normal analysis run** — when `classification_results` produces a row whose `classification` requires human approval before any purchase action (i.e., `BUY` classifications, per your business rules — `REVIEW`/`NEGOTIATE`/`NO_BUY`/`HIGH_RISK` presumably don't need a Telegram approval flow, though confirm this mapping against your actual business rules rather than assume it here). Wherever your rules engine currently finalizes a `classification_results` row, add a call that creates a corresponding `approval_queue` row (in `queued` status if something's already active, or `pending` if the queue is empty) when the classification warrants approval.
2. **From the "silently prep, then gate" conversational flow** — per `OPENCLAW_APPROVAL_STATE_MACHINE_SPEC.md` Section 5b, when a client's off-topic Telegram reply triggers new analysis work mid-conversation, the backend does all the prep work (this reuses the exact same rules/classification pipeline as #1) and then creates a new `approval_queue` row at the point where an approval-gated action would fire — never before.

**Practical implication:** both paths funnel through the same "create an approval item" function — write this once (e.g. `approvals.service.create_from_classification(classification_result_id)`) and call it from both the scheduled/batch analysis path and the ad-hoc conversational path, rather than duplicating the logic.

---

## 5. Reminder-scheduling job (adjacent infrastructure, not core backend code, but reads/writes the same table)

Per `OPENCLAW_TECHNICAL_SPEC.md` Section 5 (as updated) and the deployment plan: **the existing SQS worker cannot be reused** — it's confirmed purely event-driven (long-poll loop, no timer) and, separately, not even deployed to ECS yet. Decided approach: **EventBridge Scheduler**, firing every 1 minute, triggering a small task that:
- Queries `approval_queue` for the active item.
- Checks `pending_started_at` (5-min threshold) or `last_reminder_at` (10-min threshold, up to `reminder_count = 6`).
- If due, sends the next reminder (this calls back out to OpenClaw/Telegram — needs its own small piece of logic, likely a lightweight call to OpenClaw's Telegram-sending capability or directly to the Telegram Bot API for the reminder message itself, worth deciding during implementation which is cleaner) and increments `reminder_count` / updates `last_reminder_at`.
- If `reminder_count` hits 6 with no response, transitions status to `holding` and stops.

This can be a small standalone Lambda or ECS scheduled task reading the same Postgres database — doesn't need to live inside the main FastAPI app, but should live in the same repo for maintainability.

---

## 6. Summary of new secrets this requires from the backend side

(Full list including OpenClaw-side secrets is in `OPENCLAW_DEPLOYMENT_PLAN.md` Section 6 — this is just the one that originates from/is validated by the backend):

- `CANDELARIA_BACKEND_TOKEN` — static bearer token, generated once, stored in Secrets Manager, validated by the new MCP auth middleware (Section 3).

---

## 7. What does NOT change

Explicitly calling this out so it's not accidentally touched during implementation:
- `classification_results`, `raw_line_items`, `amazon_data_snapshots`, `product_matches`, `suppliers` — no schema changes, no new columns. `approval_queue` only reads via the single `analysis_result_ref` FK.
- The existing SQS worker (`worker.py`) — untouched; it's unrelated to this integration (it processes `process_run` jobs, not approvals).
- The existing FastAPI `backend` ECS service/task definition — the new `/mcp` routes can be added to this same service; no new ECS service required for the MCP wrapper itself (only the separate reminder-scheduling piece in Section 5 is new infrastructure).
