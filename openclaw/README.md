# OpenClaw — Telegram approval bot

This is the Telegram-facing half of the Candelaria 2014 automation
platform: a thin [OpenClaw](https://openclaw.ai) agent that lets the
client approve, deny, reprioritize, or revoke purchase recommendations
from Telegram, calling the backend in `../backend/` via MCP. It never
decides anything financial itself — see `workspace/AGENTS.md` for the
exact rules it operates under, and `../docs/openclaw/` for the full
specs this was built from (`OPENCLAW_TECHNICAL_SPEC.md`,
`OPENCLAW_APPROVAL_STATE_MACHINE_SPEC.md`,
`OPENCLAW_DEPLOYMENT_PLAN.md`).

**Status as of 2026-10-09: this directory is the application-level
scaffold only** — config, agent instructions, and the image definition.
**No infrastructure exists yet** (no ECS task definition/service, no
EFS volume, no secrets provisioned) — that's `infra/`'s job, still
open, tracked in `OPENCLAW_DEPLOYMENT_PLAN.md`'s own checklist.

## Layout

| Path | Purpose |
|---|---|
| `config/openclaw.json5` | Gateway config — source of truth, checked into git (see its own header comment for how this relates to the real runtime config file) |
| `workspace/AGENTS.md` | The agent's operating instructions — the state machine's rules translated into something the model actually reads |
| `Dockerfile` | Pins the exact OpenClaw image version used |
| `.gitignore` | Keeps credentials/session state out of git, per OpenClaw's own guidance |

## Secrets this needs (none provisioned yet)

Per `OPENCLAW_DEPLOYMENT_PLAN.md` Section 6:

| Secret | Purpose |
|---|---|
| `OPENCLAW_GATEWAY_TOKEN` | Protects the Gateway's own API (matters for Control UI/CLI access even though nothing public-facing calls it) |
| `TELEGRAM_BOT_TOKEN` | From BotFather — **already have this one**: it's the same token stored in `adc/prod/telegram-reminder`'s `bot_token` field in the main backend's Secrets Manager |
| `ANTHROPIC_API_KEY` | OpenClaw's own model access (see "Model choice" below) — **still needed, see CLAUDE.md's open item** |
| `CANDELARIA_BACKEND_TOKEN` | The bearer token the backend's MCP server checks — **already provisioned**: `adc/prod/openclaw-backend-token` |

All four are referenced in `config/openclaw.json5` via `${VAR}`
substitution, injected as ECS task-definition env vars once that
infrastructure exists — never hardcoded.

## Model choice

`config/openclaw.json5` configures `anthropic/claude-sonnet-5` as the
agent's model. This isn't incidental: OpenClaw holds this backend's
bearer token and can call `approve_decision`/`revoke_decision`, so per
`OPENCLAW_DEPLOYMENT_PLAN.md` Section 0b, model tier is a security
control here, not just a quality choice — a frontier-tier model is used
deliberately. If real usage ever shows it struggling with the tool-use
load, `anthropic/claude-opus-5` is the stronger (and pricier) fallback —
change `agents.entries.main.model` in the config.

## First boot / config seeding — read this before deploying

**Verified for real** (pulled and ran `ghcr.io/openclaw/openclaw:2026.9.9`
locally, 2026-10-09 — not assumed from docs): the image's entrypoint runs
`openclaw doctor --fix --non-interactive` on every start, which *does*
auto-generate a default config if none exists — but that default is
missing `gateway.mode`, and the Gateway then **refuses to start**
("Gateway start blocked: existing config is missing gateway.mode").

So: the real runtime config at `/home/node/.openclaw/openclaw.json` (on
the persistent EFS volume, once that exists) must be seeded from
`config/openclaw.json5` **before** the Gateway service's first start —
strip the comments to get valid JSON, and place it at that exact path
on the volume (e.g. via an ECS Exec/SSM session into a one-off task with
the EFS volume attached, before the long-running service task starts).
Once seeded, `doctor --fix` leaves a valid config alone — confirmed by
the same local run.

Don't hand-edit the runtime copy going forward; change
`config/openclaw.json5` here and re-seed, so git stays the source of
truth.

## Collecting Telegram user IDs (for `channels.telegram.allowFrom`)

OpenClaw doesn't need to be running for this — you just need the bot
(its token already exists) to receive one message from each person:

1. Have the client and Rob each open a chat with the bot and send
   anything (e.g. `/start`).
2. Call `https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates` —
   each message shows a numeric `from.id` (same as `chat.id` for a
   private 1:1 chat).
3. Add both numbers to `config/openclaw.json5`'s
   `channels.telegram.allowFrom` array, re-seed the config.

Pairing (`dmPolicy: "pairing"`) is the real access-control mechanism and
works without this; `allowFrom` is defense-in-depth on top of it, per
`OPENCLAW_TECHNICAL_SPEC.md` Section 1.

## Health check (verified, ready to paste into an ECS task definition)

The image ships three unauthenticated HTTP endpoints on port 18789,
confirmed by actually probing them locally:

```
GET /healthz   → {"ok":true,"status":"live"}
GET /startupz  → {"ok":true,"status":"started","version":"2026.9.9","uptimeMs":...}
GET /readyz    → {"ready":true,"failing":[],...}
```

Because `gateway.bind: "loopback"` (deliberate — no inbound access
needed at all, see `OPENCLAW_DEPLOYMENT_PLAN.md` Section 4), these are
only reachable from *inside* the container's own network namespace, not
from outside it — exactly what an ECS container-level health check
needs, not a public ALB check. `curl` is present in the image; this
command works as the ECS task definition's `healthCheck.command`:

```
CMD-SHELL,curl -f http://127.0.0.1:18789/healthz || exit 1
```

## Local dry run (no AWS, no real secrets)

```sh
docker build -t openclaw-local .
mkdir -p ./local-state
# Strip the JSON5 comments before copying - the real binary expects
# plain JSON at this path.
python3 -c "import json5,json; json.dump(json5.load(open('config/openclaw.json5')), open('./local-state/openclaw.json','w'))" \
  || echo "install json5 (pip install json5) or hand-strip comments first"
docker run --rm -v "$(pwd)/local-state:/home/node/.openclaw" openclaw-local
```

This boots the Gateway against the real config shape with
`channels.telegram.enabled: false`'d out if you haven't got real tokens
yet — useful for confirming config changes don't break startup before
touching anything real. Real end-to-end testing (pairing, actual
approval flow) needs the real secrets and, ideally, a stub MCP server
standing in for the production backend first — see
`OPENCLAW_DEPLOYMENT_PLAN.md` Section 9, step 6.

## What's still open

- **Infrastructure**: EFS volume, ECS task definition + service, the
  four secrets above provisioned in Secrets Manager — none of this
  exists yet (`OPENCLAW_DEPLOYMENT_PLAN.md`'s own checklist, steps 2-5).
- **`ANTHROPIC_API_KEY`**: needs provisioning + a spend limit set in the
  Anthropic Console — tracked in the main repo's `CLAUDE.md`.
- **Telegram user IDs**: needed to fill `allowFrom` — see above.
- **Stub-first testing**: no stub MCP server exists in this repo for
  the safer dry-run sequence `OPENCLAW_DEPLOYMENT_PLAN.md` recommends
  before pointing at production — `config/openclaw.json5` currently
  points straight at the real backend.
