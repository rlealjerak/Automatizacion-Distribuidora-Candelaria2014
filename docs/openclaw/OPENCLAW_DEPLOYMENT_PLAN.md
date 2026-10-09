# OpenClaw Deployment Plan — Complete, Ready for Implementation

**Project:** Distribuidora Candelaria 2014 LLC — Amazon FBA Automation Platform
**Status:** Discovery complete. Two new findings below change scope slightly — read those first.
**Depends on:** `OPENCLAW_APPROVAL_STATE_MACHINE_SPEC.md`, `OPENCLAW_TECHNICAL_SPEC.md` (this doc covers *how OpenClaw itself gets deployed*; those cover *what it does once running*)

---

## 0. Two new findings that change the plan

These weren't visible until this research pass. Flagging both clearly rather than folding them in silently.

### 0a. OpenClaw needs its own LLM provider API key — new recurring cost

OpenClaw's Gateway is an **agent runtime**, not a dumb router. It needs a model (Anthropic, OpenAI, or another supported provider) to actually interpret your Telegram messages, decide which tool to call, and generate replies. This is separate from any Claude usage elsewhere in this project — it's a distinct, ongoing API cost tied to every message the bot processes.

**Implication:** you'll need an Anthropic API key (recommended, per the security docs' guidance to use frontier-tier models for tool-enabled agents — see 0b) provisioned specifically for OpenClaw, stored in Secrets Manager, with its own usage/billing visibility. Given your $500/month total budget ceiling, this is a new line item to account for — likely modest for a single-user approval bot with low message volume, but real and worth tracking from day one rather than discovering it on a bill.

### 0b. Model choice is a security control here, not just a quality choice

The official security docs are explicit: prompt-injection resistance varies significantly by model tier, and **tool-enabled agents should use the latest, strongest model tier available**, not a cheaper/smaller one. Since OpenClaw will hold your backend's bearer token and can call `approve_decision`, this isn't a place to economize on model choice — use a current frontier-tier Claude model as OpenClaw's configured model, not a budget/older tier.

---

## 1. Deployment architecture — ECS Fargate translation

**No official AWS/ECS guide exists** (OpenClaw's docs cover Azure, GCP, Cloudflare, DigitalOcean, Hetzner, Railway, Kubernetes, and several VPS providers — AWS ECS is not among them). Everything below translates the official Docker-based deployment model onto your existing Fargate setup; this is inference from the general Docker contract, not a documented AWS path, and should be validated against real behavior when you deploy.

**Image:** Use the official pre-built image, not a local source build (the source build needs 6GB+ RAM at build time — irrelevant complexity for you). Pull from GitHub Container Registry:
```
ghcr.io/openclaw/openclaw:<version-tag>
```
Pin to an explicit version tag (e.g. `2026.2.26`) or a dated `-rYYYYMMDD` tag — **not** `latest` or `main`, which move weekly. This matches your project's general preference for explicit, reviewable infrastructure changes over floating targets.

**Task definition shape (single container, single ECS service):**
- Container image: as above
- Command: default (runs the Gateway)
- Health check: use the built-in HTTP probes (see Section 5)

---

## 2. Persistence — new required infrastructure (EFS)

**This is the most important finding in this document.** OpenClaw's Gateway is **not stateless**. It stores real, required state on local disk under `/home/node/.openclaw`:

- `openclaw.json` — your configuration
- `state/openclaw.sqlite` — shared runtime state, including MCP OAuth tokens, pairing/session data
- `credentials/<channel>-allowFrom.json` — your Telegram pairing allowlist (this is the actual allowlist file — if it's lost, you'd have to re-pair)
- `agents/<agentId>/agent/openclaw-agent.sqlite` — per-agent runtime state and session history
- `.env` — runtime secrets

Fargate tasks have **ephemeral storage by default** — if a task restarts (deploy, crash, scaling event), anything not on a persistent volume is gone. Without addressing this, every restart would wipe your Telegram pairing and force you to re-pair, and would lose session/conversation continuity.

**Required addition to the architecture: an EFS file system, mounted into the ECS task definition at `/home/node/.openclaw`.** This is new infrastructure not previously scoped anywhere in this project. Concretely:
- Create an EFS file system in the same VPC as your ECS cluster.
- Add an EFS mount target in the private subnet(s) your ECS tasks run in.
- Reference it in the task definition's `volumes` block (`efsVolumeConfiguration`), mounted to `/home/node/.openclaw` in the container definition's `mountPoints`.
- Cost impact: EFS is billed per GB stored + throughput; for this bot's tiny state footprint (config, a few SQLite files, session transcripts), this should be single-digit dollars/month — but worth confirming against your remaining budget headroom once other costs are tallied (see Section 8).

---

## 3. Compute sizing

Official minimums (VPS-oriented, so treat as a starting point, not gospel for Fargate): 2 vCPU / 4GB RAM is the commonly cited production minimum, but that figure includes headroom for browser automation (Chromium) and local source builds — neither of which applies to you. Your use case (`tools.profile: minimal`, no browser, no exec, no sandbox, low message volume, single user) is much lighter than the typical OpenClaw deployment these guides are written for.

**Recommendation: start at 0.5 vCPU / 1GB Fargate task size**, matching the "idles around 256MB for a single agent" runtime behavior noted in community deployment guides, with room to scale up if you observe memory pressure once real usage patterns are visible. This is a starting point to validate empirically, not a guarantee — watch CloudWatch memory utilization after deployment and adjust if needed.

---

## 4. Networking — simpler than initially assumed

Earlier planning assumed OpenClaw might need inbound connectivity (an ALB, a listener). **That's not the case.** OpenClaw's outbound connections are:
- Telegram (long-polling, outbound only — confirmed earlier in this project)
- Your backend's MCP endpoint (outbound HTTPS, once domain/TLS is live)
- The LLM provider API (outbound HTTPS, per Section 0a)

**No inbound access is required for the bot to function.** This means:
- ECS task can run in a **private subnet with no public IP and no ALB target group** — smaller attack surface, simpler networking, and consistent with the security docs' strong preference for keeping the Gateway's port (18789) off any public-facing listener entirely ("never expose port 18789 publicly, even with authentication").
- Security group: outbound HTTPS (443) only; no inbound rules needed for core function.

**If you want occasional access to OpenClaw's Control UI** (a web dashboard for the Gateway — optional, not required for the Telegram approval flow to work), the recommended pattern per the docs is **not** a public listener but a tunnel: AWS Session Manager port-forwarding (`aws ssm start-session` with port forwarding to the task) is the closest equivalent to the "SSH tunnel to loopback" pattern the docs recommend, since ECS Fargate doesn't support SSH directly but does support SSM Exec. This is optional and can be deferred — the approval flow itself only needs Telegram connectivity, not Control UI access.

---

## 5. Health checks

OpenClaw's image ships three unauthenticated HTTP probe endpoints, purpose-built for exactly this kind of orchestrator integration:
```
GET /healthz   — liveness
GET /startupz  — startup / traffic admission
GET /readyz    — deep, channel-aware readiness (fails if Telegram connectivity is broken)
```

**ECS task definition health check** (container-level): use `/healthz` for the standard `HEALTHCHECK` — matches the image's own built-in check.
**Recommendation:** also consider a CloudWatch alarm on repeated `/readyz` failures if you want visibility specifically into "Telegram connection is down" as distinct from "container is unhealthy," since `/readyz` is channel-aware and `/healthz` isn't — this is a nice-to-have for observability, not required for MVP.

---

## 6. Secrets inventory — everything that goes in Secrets Manager

Consolidated list, including the two new items from Section 0:

| Secret | Purpose | Injected as |
|---|---|---|
| `OPENCLAW_GATEWAY_TOKEN` | Gateway's own auth token (protects the Gateway's WebSocket/HTTP API — matters even though nothing public-facing calls it, since Control UI/CLI access uses this too) | ECS task env var |
| `TELEGRAM_BOT_TOKEN` | From BotFather, identifies your bot to Telegram | ECS task env var |
| `ANTHROPIC_API_KEY` (or chosen provider) | **New (Section 0a)** — OpenClaw's own model access for interpreting messages | ECS task env var |
| `CANDELARIA_BACKEND_TOKEN` | Static bearer token your MCP wrapper validates (per earlier decision) | ECS task env var, referenced via `${CANDELARIA_BACKEND_TOKEN}` in `openclaw.json` |

All four follow the same pattern already established for the rest of this project: Secrets Manager → ECS task definition `secrets` block → environment variable at runtime. No plaintext anywhere.

---

## 7. Gateway configuration — final `openclaw.json` skeleton

Combining the security-hardened baseline from the docs with everything decided earlier in this project (pairing, minimal tools, MCP server, toolFilter):

```json5
{
  gateway: {
    mode: "local",
    bind: "loopback",        // no inbound needed at all (Section 4)
    port: 18789,
    auth: { mode: "token", token: "${OPENCLAW_GATEWAY_TOKEN}" },
  },
  session: {
    dmScope: "main",         // single/dual-user, not a shared multi-user inbox — no isolation needed
  },
  channels: {
    telegram: {
      enabled: true,
      botToken: "${TELEGRAM_BOT_TOKEN}",
      dmPolicy: "pairing",   // confirmed true allowlist behavior (see technical spec, Section 1)
    },
  },
  tools: {
    profile: "minimal",      // overrides the default "coding" profile — no fs/exec/browser tools
    deny: ["gateway", "cron", "sessions_spawn", "sessions_send", "exec", "process", "browser", "web_fetch", "web_search"],
    elevated: { enabled: false },
  },
  mcp: {
    servers: {
      candelaria_backend: {
        url: "https://api.distribuidoracandelaria2014ops.com/mcp",  // real domain/TLS live as of 2026-10-05
        transport: "streamable-http",
        requestTimeoutMs: 20000,
        connectionTimeoutMs: 5000,
        headers: { Authorization: "Bearer ${CANDELARIA_BACKEND_TOKEN}" },
        toolFilter: { include: ["list_queue", "approve_decision", "reorder_queue", "revoke_decision"] },
      },
    },
  },
}
```

Notes on choices baked into this config:
- `bind: "loopback"` — correct given Section 4's finding that no inbound access is needed; Control UI access (if ever wanted) goes through SSM tunneling to loopback, not a LAN/public bind.
- `tools.deny` explicitly blocks `exec`, `process`, `browser`, `web_fetch`, `web_search` on top of the `minimal` profile — belt-and-suspenders, since this bot's entire job is relaying approvals, not browsing the web or running shell commands. This directly follows the security docs' "reduce blast radius" guidance for a narrowly-scoped agent.
- No sandbox config needed — sandboxing exists to isolate tool execution (exec/browser/fs), all of which are denied outright here. Sandbox would be relevant if this bot ever gained broader tool access; it doesn't need to for its current job.

---

## 8. Budget check-in

New recurring costs surfaced in this document, beyond what's already running:
- EFS (Section 2): likely single-digit $/month for this workload's tiny state footprint.
- Fargate task (Section 3): a 0.5 vCPU/1GB task running 24/7 is a modest, predictable monthly cost — roughly comparable in scale to your existing small ECS services, worth checking against your current AWS bill total rather than me asserting an exact figure I can't verify from here.
- LLM provider API usage (Section 0a): variable, depends on message volume — for a single-user low-frequency approval bot this should be small, but it's genuinely new and unbudgeted until now.

Given your stated ~$500/month ceiling, none of this looks likely to be a problem on its own, but it's worth a quick tally against your actual current AWS + API spend before committing, since it's easy for several "should be small" items to add up unnoticed.

---

## 9. Full sequential implementation checklist

Everything needed to go from "nothing deployed" to "OpenClaw running on ECS, paired to Telegram, talking to a stub backend" — Phase 2 per the original discovery-prompt's phasing, still gated from real backend traffic until domain/TLS clears.

1. **Telegram bot registration** — message @BotFather, register the bot, obtain `TELEGRAM_BOT_TOKEN`.
2. **Provision secrets** — create all four Section 6 secrets in Secrets Manager under a consistent path (e.g. `candelaria/openclaw/*`).
3. **Create EFS file system** — in the existing VPC, with a mount target in the private subnet(s) used by ECS tasks; security group allowing NFS (2049) from the ECS tasks' security group only.
4. **Build/select the ECS task definition** — container from `ghcr.io/openclaw/openclaw:<pinned-tag>`, EFS volume mounted to `/home/node/.openclaw`, the four secrets injected as env vars, health check on `/healthz`, sized per Section 3.
5. **Create the ECS service** — private subnet, no public IP, no ALB target group (per Section 4), security group allowing outbound HTTPS only.
6. **Write `openclaw.json`** per Section 7, with the MCP `url` pointed at a **stub backend** (not production) for this phase — a minimal local/test MCP server implementing `list_queue`/`approve_decision`/`reorder_queue` with fake data, so the full loop (Telegram → OpenClaw → MCP tool call → response) can be tested end-to-end before touching anything real.
7. **Deploy and verify container health** — confirm `/healthz` and `/startupz` pass; check CloudWatch logs for clean startup.
8. **Pair your Telegram account** — message the bot, receive the pairing code, approve it via `openclaw pairing approve telegram <CODE>` (run through ECS Exec/SSM into the running task, since there's no local CLI access to this container).
9. **Test the stub loop** — trigger a fake pending item against the stub MCP server, confirm the full state-machine behavior (initial message → 5 min wait → reminders → holding) works as specified in `OPENCLAW_APPROVAL_STATE_MACHINE_SPEC.md`.
10. **Hold at this point** until domain/TLS is confirmed live, then swap the MCP `url` to the real backend endpoint (one-line config change) and re-verify against real (but low-stakes/test) data before trusting it with real approval flows.

---

## 10. Still open (small, and none blocking)

- Exact Fargate task size (Section 3) is a starting estimate — validate against real CloudWatch metrics post-deploy.
- Whether Control UI / SSM tunnel access is wanted at all — optional, can be decided later without affecting the core build.
- Which LLM provider/model to configure for OpenClaw's own reasoning (Section 0b strongly suggests current frontier-tier Claude, given tool access) — a explicit choice to confirm before step 2 of the checklist, since it determines which API key gets provisioned.
