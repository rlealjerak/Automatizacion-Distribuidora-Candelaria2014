# OpenClaw ↔ Backend Integration — Technical Spec v1

**Project:** Distribuidora Candelaria 2014 LLC — Amazon FBA Automation Platform
**Status:** Ready for implementation review. Approval state machine (Section 3) is LOCKED per owner sign-off; other sections are implementation-ready but should be reviewed against the actual backend codebase before build starts.
**Depends on:** `OPENCLAW_APPROVAL_STATE_MACHINE_SPEC.md` (this document incorporates and extends it)

---

## 1. Access control — confirmed from official docs

Source: `docs.openclaw.ai/channels/pairing` (read in full, not summarized).

OpenClaw's Telegram access control is **DM pairing**, a built-in mechanism — no custom allowlist code needed:

- Set `channels.telegram.dmPolicy: "pairing"` (this is already the Telegram default).
- When an unknown Telegram user messages the bot, OpenClaw generates an 8-character pairing code (uppercase, no ambiguous characters) and does **not process their message** until you approve.
- Pairing codes expire after 1 hour. Pending requests cap at 3 per channel account.
- You approve via CLI (`openclaw pairing approve telegram <CODE>`) or Control UI (Settings → Channels → DM access requests).
- Approved senders are stored in the shared SQLite state (`channel_pairing_allow_entries`), keyed by channel + account.
- **This is a true allowlist, not "first message wins."** Only approved senders' messages get processed at all. This satisfies the "not an open bot" requirement directly — no additional `allowFrom` config layer is strictly required, though one can be added for belt-and-suspenders (see below).

**For your single/dual-user model:**
- You message the bot once, it issues you a pairing code, you approve it (via CLI on the ECS host, or Control UI if exposed) — done. This makes you the approved sender.
- If a second person needs testing access later, same flow: they message the bot, you approve their code explicitly.
- **Recommendation:** in addition to relying on `pairing`, set an explicit `channels.telegram.allowFrom` list (via the `accessGroups` mechanism or directly) once your Telegram numeric user ID is known, as defense-in-depth — this means even if pairing state were somehow reset, an explicit allowlist still blocks unknown senders. Not required, but cheap and consistent with the project's stated preference for explicit allowlists over implicit trust.
- **First command owner:** the CLI approval flow auto-bootstraps `commands.ownerAllowFrom` on first approval if no owner is set — worth confirming this resolves to your account and not accidentally to a test account during initial setup.

**Still to confirm once you're actually configuring this (not blocking today):** whether `commands.ownerAllowFrom` privileges (exec approval prompts, privileged commands) should be limited given this bot's sole job is approvals/queue management — likely want a restrictive `tools.profile` regardless of owner status (see Section 5).

---

## 2. MCP connection — confirmed configuration shape

Source: `docs.openclaw.ai/gateway/configuration-reference`, `mcp.servers` schema.

```json5
{
  mcp: {
    servers: {
      candelaria_backend: {
        url: "https://api.distribuidoracandelaria2014ops.com/mcp",   // real domain/TLS live as of 2026-10-05
        transport: "streamable-http",
        requestTimeoutMs: 20000,
        connectionTimeoutMs: 5000,
        headers: {
          Authorization: "Bearer ${CANDELARIA_BACKEND_TOKEN}",
        },
        toolFilter: {
          include: ["list_queue", "approve_decision", "reorder_queue"],
        },
      },
    },
  },
  tools: {
    profile: "minimal",              // start restrictive; only session_status by default
    allow: ["bundle-mcp"],           // explicitly allow the configured MCP server's tools
  },
}
```

Notes:
- `${CANDELARIA_BACKEND_TOKEN}` resolves via OpenClaw's env-var substitution — sourced from the ECS task definition's injected Secrets Manager value. No plaintext token in config.
- `toolFilter.include` restricts OpenClaw's visibility to exactly the three tools this bot needs — nothing else from the backend is exposed even if the MCP wrapper adds more tools later without updating this filter.
- `tools.profile: "minimal"` overrides the framework's default `"coding"` profile (which would otherwise expose filesystem/exec/web tools this bot has no business having). This is a deliberate hardening step beyond what's strictly required — the approval bot should not be able to run shell commands or browse the web.
- **Stub-first sequencing (per your item #1 decision):** point `url` at a local/stub MCP server during Phase 2 (base setup), swap to the real `https://` backend URL only once domain/TLS is confirmed live. This is a one-line config change, not a redeploy of logic.

---

## 3. Approval state machine — LOCKED

See `OPENCLAW_APPROVAL_STATE_MACHINE_SPEC.md` for the full state diagram and rules. Summary for reference:

- 5 min initial wait → up to 6 reminders every 10 min → indefinite `holding`. No quiet hours. Never auto-approve/auto-deny.
- Off-topic replies: non-financial → execute immediately; financial-eventually → silently prep, gate at the approval step, queue behind current item.
- Explicit reprioritization only; nothing reorders itself.
- One active item at a time; reminders batch if this model is ever loosened to multiple concurrent active items (not currently the case).

---

## 4. MCP tool schemas

These are the three tools your backend's MCP wrapper must expose (via `@modelcontextprotocol/sdk`), matching what OpenClaw's tool-calling contract expects: a tool name, a JSON Schema for input, and a structured result.

### 4.1 `list_queue`

Returns the current active item (if any) and all queued items, in order.

**Input schema:**
```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```
No parameters — always returns full current state.

**Output (tool result content):**
```json
{
  "active_item": {
    "id": "apr_8f3a2b",
    "type": "purchase_recommendation",
    "status": "reminder_2",
    "summary": "Buy 240 units of ASIN B0XXXXXX from Proveedor Rivas — ROI 34%, margin 18%, est. profit $1,240",
    "created_at": "2026-08-23T14:02:11Z",
    "pending_started_at": "2026-08-23T14:05:11Z",
    "last_reminder_at": "2026-08-23T14:35:11Z",
    "reminder_count": 2,
    "detail_url": "https://api.distribuidoracandelaria2014ops.com/approvals/apr_8f3a2b"
  },
  "queued_items": [
    {
      "id": "apr_9a1c4d",
      "type": "purchase_recommendation",
      "queue_position": 1,
      "summary": "Buy 90 units of ASIN B0YYYYYY from Proveedor Lopez — ROI 22%, margin 12%",
      "created_at": "2026-08-23T14:20:03Z"
    }
  ]
}
```

Notes:
- `type` field allows future decision types beyond purchase recommendations (e.g., replenishment orders in Phase 2) without changing the schema shape.
- `summary` is the human-readable string OpenClaw relays directly into the Telegram message — backend owns generating a clear, complete summary; OpenClaw should not need to reformat or interpret raw data.
- `detail_url` optional, for a future web view; not required for MVP.

### 4.2 `approve_decision`

Resolves the **active item only**. Backend must reject calls targeting a non-active item ID (enforces the one-active-item model server-side, not just trusting OpenClaw's logic).

**Input schema:**
```json
{
  "type": "object",
  "properties": {
    "approval_id": { "type": "string" },
    "resolution": { "type": "string", "enum": ["approved", "denied"] },
    "note": { "type": "string", "description": "Optional free-text context from the client's reply" }
  },
  "required": ["approval_id", "resolution"],
  "additionalProperties": false
}
```

**Output:**
```json
{
  "ok": true,
  "approval_id": "apr_8f3a2b",
  "resolution": "approved",
  "resolved_at": "2026-08-23T14:36:40Z",
  "next_active_item": {
    "id": "apr_9a1c4d",
    "summary": "Buy 90 units of ASIN B0YYYYYY from Proveedor Lopez — ROI 22%, margin 12%"
  }
}
```

Error case (wrong/non-active ID):
```json
{
  "ok": false,
  "error": "approval_id apr_XXXX is not the active item",
  "active_approval_id": "apr_8f3a2b"
}
```

This error path matters: it's the server-side backstop that prevents a bug or race condition in OpenClaw from resolving the wrong item — the backend never trusts the caller's claim about which item is active.

### 4.3 `reorder_queue`

Moves a specified queued item to the front of the queue (i.e., it becomes next after the current active item resolves). Only invoked when the client has explicitly said to prioritize something — this is a design/prompt-level constraint on OpenClaw's behavior, not something the schema alone can enforce, so it must also be reinforced in the OpenClaw agent's instructions/system prompt.

**Input schema:**
```json
{
  "type": "object",
  "properties": {
    "approval_id": { "type": "string" }
  },
  "required": ["approval_id"],
  "additionalProperties": false
}
```

**Output:**
```json
{
  "ok": true,
  "approval_id": "apr_9a1c4d",
  "new_queue_position": 1
}
```

### 4.4 (Internal, not OpenClaw-facing) `create_queued_item`

Not exposed to OpenClaw as an MCP tool — this is invoked by the backend's own analysis pipeline when a new approval-requiring recommendation is generated (either from the daily list-analysis run, or from the "silently prep, gate at approval" flow in Section 3/5b of the state machine spec). Included here for completeness since it's part of the same data flow, but it's backend-internal, not something OpenClaw calls.

---

## 5. Backend data model

New table: `approval_queue` (deliberately distinct from any existing "decision"/classification concept — see note below).

**This is workflow-tracking state, not analysis output.** Priority-1's classification (Buy / Review / Negotiate / Do not buy / High risk) is a business-rules conclusion and lives entirely in the existing analysis schema — this new table has no bearing on that and doesn't need to match its conventions. `approval_queue` exists solely to track where a given approval-requiring item sits in the Telegram back-and-forth: is it currently waiting on Rob, how many reminders have gone out, what's queued behind it. The only link between the two is a single foreign key per row pointing back at the relevant analysis-result row — nothing else needs to align.

Note: the `approval_id` field name used in the MCP tool schemas (Section 4) refers to this approval-queue row's ID — an approve/deny decision on whether to proceed with a recommendation — not the Priority-1 classification. Different "decision," same word; worth flagging to avoid confusion during implementation.

| Field | Type | Notes |
|---|---|---|
| `id` | string/UUID | Primary key |
| `type` | enum/string | `purchase_recommendation` for MVP; extensible for Phase 2+ |
| `status` | enum | `queued`, `pending`, `reminder_1`...`reminder_6`, `holding`, `approved`, `denied` |
| `queue_position` | integer, nullable | Null when active; integer order among `queued` items |
| `summary` | text | Human-readable summary for Telegram display |
| `analysis_result_ref` | UUID, FK → `classification_results.id` | Confirmed against real schema — see note below the table. |
| `summary_payload` | JSON | A denormalized snapshot of just the fields needed to render the Telegram summary (ASIN, ROI, margin, cost, supplier) — not the full analysis record, to avoid this table becoming a second copy of analysis data. |
| `created_at` | timestamp | |
| `pending_started_at` | timestamp, nullable | Set when item becomes active; resets the 5-min/reminder clock |
| `last_reminder_at` | timestamp, nullable | |
| `reminder_count` | integer, default 0 | |
| `resolved_at` | timestamp, nullable | |
| `resolution` | enum, nullable | `approved` / `denied` |
| `resolution_note` | text, nullable | Captured from client's free-text reply if any |

**Relationship to existing MVP schema:** deliberately minimal — one FK column (`analysis_result_ref`), nothing else. This table's naming, enum styles, and structure are internal to the approval workflow and do not need to match the analysis schema's conventions.

**Confirmed target (verified against real codebase, 2026-08-23):** there is no single denormalized analysis-results table — the schema is normalized. The closest match, and the correct FK target, is `classification_results` (`backend/src/adc_backend/modules/rules/models.py`): primary key `id`, type `UUID`, single column (not composite). This table itself holds the classification label, ROI, margin, and rule trace; it references `raw_line_items` (supplier item number, cost, case pack), `amazon_data_snapshots` (ASIN, pricing, fees, sales rank), and `product_matches` (ASIN mapping) via its own FKs — so pointing `analysis_result_ref` at `classification_results.id` gives `approval_queue` a path to all the related data needed for `summary_payload` generation, without needing multiple FK columns.
Note: the classification enum in the real schema is English (`BUY`/`REVIEW`/`NEGOTIATE`/`NO_BUY`/`HIGH_RISK`), not the Spanish terms used in the client brief — worth being consistent with the real enum in implementation.

**Reminder scheduling:** requires a scheduled job that periodically checks `pending_started_at`/`last_reminder_at` against the 5-min/10-min thresholds and triggers the next reminder.

**Correction to a prior assumption:** this spec previously assumed the existing SQS worker was already deployed to ECS and could be extended to also handle periodic reminder checks. That assumption was wrong on both counts, confirmed against the real codebase (2026-08-23):
- The SQS worker (`backend/src/adc_backend/worker.py`) is **not deployed to ECS** — no task definition or service exists for it in `infra/`. It currently only runs locally for manual testing. The one ECS service that does exist (`backend`, in `infra/modules/ecs_cluster/main.tf`) is the FastAPI HTTP API, not the worker.
- Even once deployed, the worker is **purely event-driven** — a long-poll loop (`receive_message` with `WaitTimeSeconds=20`) with no timer, cron, or interval-based trigger of any kind. It reacts only to messages arriving on the queue; it has no mechanism to "wake up every minute and check something" on its own.

**Implication:** reminder scheduling needs its own mechanism entirely, separate from the SQS worker. This is now a standalone open item rather than an extension of existing infrastructure — see Section 6.

---

## 6. Open items before implementation starts

1. **MCP client retry/timeout behavior** — checked against official docs (`docs.openclaw.ai/concepts/retry`). That page documents OpenClaw's retry policy for *outbound provider/channel calls* (message send, media upload, reaction, poll, sticker — retries on 429/timeout/connect-reset/unavailable, using `retry_after` or exponential backoff). It does **not** state whether a timeout on a configured MCP tool call (`mcp.servers.*`, governed by `requestTimeoutMs`/`connectionTimeoutMs`) is automatically retried by OpenClaw, or simply surfaces as a failed tool result for the agent to react to. This remains genuinely unconfirmed — no page describes MCP tool-call-level retry behavior specifically, as opposed to config for the timeouts themselves.
   **Resolution: build for the safe case rather than wait on this.** `approve_decision` must be idempotent regardless of the answer — a duplicate call with the same `approval_id` + `resolution` returns the same success result rather than erroring or double-executing. This makes the behavior safe whether OpenClaw retries automatically or a human just sees an error and replies "approve" again.
2. **`analysis_result_ref` target — RESOLVED.** Confirmed against the real schema: `classification_results.id` (UUID, single-column PK). See Section 5 for full detail on the related tables it joins to.
   - **Migration mechanism — RESOLVED.** Alembic (`backend/alembic/versions/`). New tables are defined as SQLAlchemy models under `modules/<module>/models.py`, then `alembic revision --autogenerate`, hand-edited for Postgres-native-ENUM quirks (autogenerate misses new enum values / doesn't drop enum types on downgrade — documented in `docs/decisions/0002-database-schema-decisions.md`), then `alembic upgrade head`. This runs automatically on every container start via `entrypoint.sh`. `approval_queue` should follow this exact pattern — likely living in a new `modules/approvals/models.py` given it's a distinct concern from the existing modules (ingestion, matching, amazon, rules).
3. **Reminder scheduling mechanism — DECIDED.** EventBridge Scheduler firing every 1 minute → triggers a small task (Lambda or ECS RunTask, implementation detail TBD) that checks `approval_queue` for items past their `pending_started_at`/`last_reminder_at` threshold and sends the next reminder. Chosen over extending the FastAPI service's own process (avoids coupling reminder reliability to API deploys/restarts) and over a dedicated always-on ECS service (avoids new persistent infrastructure for what's fundamentally a periodic check). This is consistent with the project's existing pattern of dedicated, single-purpose infrastructure pieces per concern.
4. **OpenClaw agent prompt/instructions** — the reprioritization-only-on-explicit-request rule (Section 3, item 5c of the state machine) and the "never silently pass the approval gate" rule need to be written into OpenClaw's system prompt/agent config, not just assumed from the tool schema. This is a Phase 2 (base setup) task, not blocking now.
5. **Domain/TLS status** — `mcp.servers.candelaria_backend.url` stays pointed at a stub until this resolves; no other blocker.
