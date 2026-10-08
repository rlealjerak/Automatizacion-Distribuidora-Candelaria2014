# OpenClaw Approval State Machine — Locked Spec v1

**Project:** Distribuidora Candelaria 2014 LLC — Amazon FBA Automation Platform
**Component:** OpenClaw (Telegram orchestration layer) ↔ Backend (FastAPI) approval flow
**Status:** LOCKED — approved by business owner. Any change to this document requires explicit owner sign-off before implementation changes.
**Governing principle:** No financially consequential action ever executes automatically. This document defines the *only* path by which a pending decision may transition to `approved` or `denied`: an explicit, unambiguous reply from the paired Telegram user to that specific pending item.

---

## 1. Actors

- **Backend** — FastAPI service, owns all state, all financial calculations, and the queue of pending decisions. Source of truth.
- **OpenClaw** — thin Telegram client. Calls backend via MCP tools. Never independently decides anything financial. Never a second source of truth.
- **Client (Rob)** — the single paired Telegram user (see Section 6, Pairing). May grow to two users during testing, but the design must not assume more than one active decision-maker for approval purposes.

---

## 2. Item states

| State | Meaning |
|---|---|
| `queued` | Item has been created (fully prepared, all non-financial work done) but is not yet the active item being presented for approval. Sits behind the current `pending`/`reminder_N`/`holding` item. |
| `pending` | Item is the active approval item. Initial Telegram message has been sent. Clock started. |
| `reminder_1` … `reminder_6` | Active item, no response yet, N reminders have been sent so far. |
| `holding` | Active item, all 6 reminders exhausted, no further messages sent. Waits indefinitely for the client to respond. |
| `approved` | Client explicitly approved this item. Terminal state. Backend executes the approved action. |
| `denied` | Client explicitly denied this item. Terminal state. No action executes. |

Only one item is ever in `pending`/`reminder_N`/`holding` at a time. All other not-yet-resolved items sit in `queued`, in order.

---

## 3. Timing rules

- **Initial wait:** 5 minutes after the `pending` message is sent, with no reply → send `reminder_1`.
- **Reminder cadence:** every 10 minutes thereafter → `reminder_2` … `reminder_6`.
- **Reminder cap:** 6 reminders maximum (60 minutes of active nudging after the initial 5-minute wait). After the 6th unanswered reminder, transition to `holding`.
- **Holding:** no further messages of any kind are sent for this item. The client must initiate contact (respond in Telegram) to resume. No timeout, no expiry.
- **No quiet hours.** Reminders are not suppressed at any time of day.
- **No auto-approval or auto-denial under any circumstance, at any state, for any reason.** This applies even if reminders are exhausted, even if the client is unreachable for days, even if a queued item behind it is time-sensitive.

---

## 4. Batching

If more than one item is simultaneously eligible for a reminder (only possible if the active-item model above is ever relaxed — under the current one-active-item design this reduces to: reminders for the single active item never fan out into multiple messages), reminders are sent as **one batched Telegram message** listing all outstanding items, not as separate messages per item. This rule is written for forward compatibility in case the "one active item" constraint is loosened later (e.g., independent reminder clocks per item); it does not currently trigger under the single-active-item model in Section 2.

---

## 5. Off-topic replies during an active approval

When the client replies to a `pending`/`reminder_N`/`holding` item with something that is **not** a resolution of that item (not approve/deny), the reply is never discarded and never silently treated as approval/denial of the pending item. It is handled as follows:

### 5a. Reply requests something that does NOT require approval
(status check, informational query, "what's the inventory on X", "pull up supplier Y's info," etc.)

→ **Execute immediately.** No queueing, no gate. This is ordinary conversational/informational work and carries no financial consequence.

### 5b. Reply requests something that WILL eventually require approval
(e.g., "also start analyzing this new supplier list," which could surface buy recommendations)

→ **Do all preparatory work silently**: data gathering, ASIN matching, ROI/margin calculation, drafting, classification — everything up to and including generating the recommendation itself.
→ **Stop exactly at the point an approval-gated action would fire.**
→ **Create a new item in `queued` state**, placed **behind** the currently active pending item.
→ The currently active pending item is unaffected and continues its own timer/reminder cycle unchanged.

### 5c. Reprioritization
The client may explicitly instruct the system to move a `queued` item ahead of the current active item (e.g., "make that one priority"). This is the **only** mechanism by which queue order changes. Nothing reorders itself automatically, regardless of urgency, deadlines, or any other signal.

### 5d. Hard boundary (non-negotiable)
Regardless of 5a/5b/5c, **the approval gate for any financially consequential action is never silently passed.** "Silently execute" in 5b refers exclusively to non-financial preparatory work (analysis, calculation, drafting). It never means executing the financially consequential action itself without the client's explicit approval of that specific item.

---

## 6. Queue advancement

When the active item resolves (`approved` or `denied`):
1. The active item's timers/reminders stop.
2. The next item in `queued` order (respecting any explicit reprioritization from 5c) becomes the new active item, transitions to `pending`, and its own 5-minute-then-reminder cycle begins fresh.
3. If no items remain in `queued`, the system has no active pending item until a new one is created.

---

## 7. State diagram (textual)

```
                 ┌─────────┐
                 │ queued  │◄────────────────────┐
                 └────┬────┘                      │ (reprioritize per 5c
                      │ becomes active             │  moves an item here)
                      ▼                            │
                 ┌─────────┐   5 min, no reply     │
                 │ pending │───────────────────►┌──┴────────┐
                 └────┬────┘                    │reminder_1 │
                      │                          └────┬──────┘
        explicit      │                               │ 10 min, no reply
        approve/deny  │                               ▼
        reply ────────┼───────────────────►      reminder_2 ... reminder_6
                      │                               │
                      ▼                               │ 6th reminder unanswered
              ┌───────────────┐                       ▼
              │ approved /    │◄───────────────── holding (indefinite,
              │ denied        │   explicit reply      no further messages,
              │ (terminal)    │   at any point         waits for client)
              └───────────────┘
```

Off-topic replies (Section 5) do not appear on this diagram because they never transition the active item's state — they either execute immediately (5a) or spawn a new `queued` item (5b) while the active item's state is untouched.

---

## 8. Implications for backend data model

The backend needs a persistent queue/state representation, not just a flat "today's list." Minimum fields per decision item:

- `id`
- `status` (`queued` | `pending` | `reminder_1`...`reminder_6` | `holding` | `approved` | `denied`)
- `queue_position` (order among `queued` items; the active item has no position, it's simply "current")
- `created_at`
- `pending_started_at` (when it became the active item — resets the 5-min/reminder clock)
- `last_reminder_at`
- `reminder_count`
- `resolved_at`, `resolution` (`approved`/`denied`), once terminal

This is an addition to whatever schema already exists for MVP list-analysis results (Priority 1 module) — worth checking against the current backend implementation to see what overlaps versus what's net-new.

---

## 9. Implications for MCP tool surface

Minimum tool set exposed by the backend's MCP wrapper to OpenClaw:

- **`list_queue`** — returns the active item (with its current state/reminder count) plus all `queued` items in order.
- **`approve_decision(id, decision)`** — resolves the active item only; backend must reject attempts to resolve a non-active item to enforce the one-active-item model.
- **`reorder_queue(id)`** — moves a specified `queued` item to the front (i.e., makes it next-in-line after the current active item resolves), per explicit client instruction only (Section 5c). This does not preempt the currently active item.
- **`create_queued_item(...)`** — used internally when 5b fires (a new approval-requiring item generated mid-conversation) to enqueue behind the current active item.

Auth: static bearer token (per owner decision), stored in Secrets Manager, injected via ECS task-definition env var, referenced in `openclaw.json` via `${CANDELARIA_BACKEND_TOKEN}` substitution into the MCP server's `headers.Authorization` field. The MCP wrapper itself is responsible for validating this token on every request.

---

## 10. Explicitly out of scope for this spec

- Multi-user approval routing (this spec assumes a single decision-maker; see project pairing decision).
- What counts as "requires approval" vs. "does not require approval" for a given action type — that classification lives in the backend's business rules engine (ROI/margin/velocity rules etc.), not in this state machine. This spec only governs *how* an approval-requiring item moves through the queue once classified as such.
- Retry/timeout behavior at the OpenClaw↔backend MCP transport layer (separate from the Telegram-facing reminder cadence in this document) — still pending verification against OpenClaw's client documentation.
