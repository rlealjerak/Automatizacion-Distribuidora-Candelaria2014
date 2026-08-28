# Distribuidora Candelaria 2014 — Next Phase Implementation Plan

**Prepared for:** Claude Code
**Prepared by:** Rob (owner/technical lead) + Claude (architecture/planning)
**Status:** Ready for implementation, sequential — do not start Goal 2 until Goal 1's Definition of Done is met.

**Scope note:** This plan covers backend infrastructure only. OpenClaw has not been configured or set up at all yet — it is intentionally out of scope here. Connecting OpenClaw to this backend is a future initiative that needs its own discovery/setup pass (installation, base configuration, understanding its actual tool-calling mechanism) before it can be turned into a concrete implementation plan like this one. It remains on the roadmap, just not as a goal in this document.

**Non-negotiable principle carried into every goal below:** no financially consequential action ever executes automatically. Nothing in this plan changes that — both goals are infrastructure/connectivity work, not business-logic changes.

---

## Context (read first)

The backend (Python/FastAPI on AWS ECS Fargate, RDS Postgres, S3, SQS, Secrets Manager) is deployed and reachable over plain HTTP via an ALB. Auth (`X-Api-Key`) is enforced. RDS now has `backup_retention_period = 7` (confirmed live and in Terraform state — no drift). The following are still open:

- No TLS — ALB is HTTP-only, API key currently travels in plaintext. **Blocked on domain registration, which is pending AWS account verification (~24hr).**
- SQS worker (`worker.py`) is code-complete but never deployed as its own service — large-list processing currently runs synchronously through the HTTP request path, which risks timeouts/data loss on real supplier-sized lists (5,000+ rows).
- OpenClaw (the Telegram/conversational layer) has not been set up or configured at all yet. It is not addressed in this plan — see the "Beyond this plan" section at the end.

---

## Goal 1: Deploy the SQS worker as an independent ECS Fargate service

### Objective
Move large-list processing off the synchronous HTTP request path and onto a durable background worker that consumes from the existing `adc-prod-list-processing` SQS queue, with failures routing to the existing `adc-prod-list-processing-dlq` dead-letter queue.

### Why this matters
The client brief requires processing 5,000+ row lists "sin intervención manual" (without manual intervention). Synchronous HTTP processing will time out on the ALB well before a real list of that size finishes, and any mid-process crash currently loses all progress with no retry. This is a correctness gap, not a nice-to-have.

### Preconditions to verify before writing any infrastructure code
1. Confirm `worker.py`'s actual behavior: does it long-poll SQS in a loop (`while True`), or is it a one-shot invocation expecting external orchestration (e.g. Lambda-style)? This determines whether it's deployed as a long-running ECS **service** (correct for a poll loop) or an ECS **scheduled task** (correct for one-shot).
2. Confirm which environment variables/secrets it reads (compare against what the API task definition already injects from Secrets Manager — likely shares `sp_api_secret_name`, `keepa_secret_name`, RDS credentials).
3. Confirm it imports from the same `app/` package the API uses (matching engine, rule engine, SP-API/Keepa clients) — if so, it can likely share the existing Docker image (different `CMD`, not a different image) rather than needing a second image built and maintained separately.

### Implementation steps
1. **Dockerfile/entrypoint:** If the worker shares the API's dependency set, add a second `CMD`/entrypoint variant to the existing image (e.g. via an environment variable or a separate entrypoint script) rather than duplicating the whole image build. Confirm `alembic/` and any other directories the API Dockerfile had to be fixed to include (per the last session's bug) are also present for the worker if it needs them.
2. **ECR:** Confirm the worker's image (or shared image with worker entrypoint) is pushed to the existing `adc-prod-backend` ECR repo, tagged distinctly (e.g. `:worker-latest` or a shared tag with entrypoint override at the ECS task-definition level — prefer the latter to avoid image duplication).
3. **Terraform additions:**
   - New `aws_ecs_task_definition` for the worker (reuse the existing image, override `command`/`entrypoint` to run `worker.py` instead of the API server).
   - New `aws_ecs_service` (no load balancer attachment needed — this service doesn't receive inbound HTTP traffic).
   - IAM task role: must include `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` on `adc-prod-list-processing`, and `sqs:SendMessage` on the DLQ if the worker handles its own dead-lettering (otherwise rely on SQS's native redrive policy — confirm which pattern `worker.py` expects).
   - Confirm the existing SQS queue already has a redrive policy pointing at the DLQ (check `infra/*.tf` for `redrive_policy` on `adc-prod-list-processing`) — if not, add one (e.g. `maxReceiveCount = 3`).
   - Set desired task count to 1 initially (not autoscaled yet — that's a future optimization once real load is observed).
4. **Update the upload endpoint (if not already wired this way):** Confirm the API's file-upload route pushes a message to SQS and returns immediately (e.g. `202 Accepted` with a job/run ID) rather than processing inline. If it currently processes synchronously, this needs to change as part of this goal — check `app/api/routes` for the upload handler.
5. **Add a way to check job status:** If not already present, the API needs an endpoint (e.g. `GET /runs/{run_id}`) so the caller (eventually OpenClaw) can poll for completion, since the upload response is now async. Check if this already exists from the original 12-epic build.

### Testing
- Deploy to the existing `adc-prod-cluster`.
- Re-run the same 5-row synthetic test file used in the prior end-to-end pipeline verification — confirm it now flows: upload → `202` response → message appears/disappears from SQS (visible in console) → worker processes it → results land in the same place as before (DB rows, review queue, etc.) → status endpoint reflects `complete`.
- Force a failure case (e.g. temporarily break one row deliberately, or simulate by pointing at an invalid ASIN) and confirm it lands in the DLQ after the configured retry count, rather than silently vanishing or crash-looping the worker.

### Definition of Done
- Worker runs as its own ECS service, independently visible in the console, consuming from `adc-prod-list-processing`.
- Upload endpoint returns immediately and processing happens asynchronously.
- A status-check mechanism exists for callers to know when a run is done.
- A deliberately-failing test message correctly lands in the DLQ rather than looping or vanishing.
- `terraform plan` shows no drift after deployment.
- Nothing about this changes any financial/purchasing logic — this is purely an execution-model change (sync → async).

---

## Goal 2: Finalize TLS/HTTPS

### Objective
Encrypt all traffic to the backend, including the `X-Api-Key` header, by attaching a domain-validated ACM certificate to the ALB and enforcing HTTPS.

### Precondition (external, not code)
Domain registration must show as `Registered` (not `Pending`) in Route53. **Do not start this goal until Rob confirms this.** This is currently blocked on AWS account verification, expected within ~24 hours of registration attempt.

### Implementation steps (once domain is confirmed registered)
1. **Confirm the Route53 hosted zone exists** for the domain (auto-created on registration) — no manual DNS delegation needed since registration went through Route53 directly.
2. **Request an ACM certificate** for the relevant subdomain (e.g. `api.<domain>.com`) via Terraform (`aws_acm_certificate` resource), using DNS validation (not email validation — DNS is automatable and doesn't depend on a mailbox existing).
3. **Add the DNS validation record** — with the hosted zone already in Route53, this can be done via `aws_route53_record` referencing the cert's validation options, fully in Terraform (no manual console step needed).
4. **Wait for validation** (`aws_acm_certificate_validation` resource) — typically resolves within minutes given Route53 is already authoritative for the zone.
5. **Add an HTTPS (443) listener to the existing ALB**, attaching the validated certificate, forwarding to the same target group the HTTP listener currently uses.
6. **Redirect HTTP → HTTPS**: change the existing port-80 listener from forwarding traffic to issuing a 301 redirect to the HTTPS listener, so plaintext requests are never actually processed, only redirected.
7. **Add an `aws_route53_record`** (A/ALIAS record) pointing `api.<domain>.com` at the ALB's DNS name, so the API has a stable, memorable hostname instead of the raw ALB address.
8. **Update the API key rotation/documentation**: since the key has been traveling in plaintext since deployment, treat this as a natural point to rotate the `X-Api-Key` value in Secrets Manager as a precaution — cheap to do, removes any residual exposure window.

### Testing
- Confirm `https://api.<domain>.com/health` (or equivalent health check route) returns `200` with a valid cert (no browser/curl warnings).
- Confirm `http://api.<domain>.com/...` redirects (301) to the HTTPS equivalent rather than serving content directly.
- Confirm the raw ALB DNS name still works during transition but plan to reference only the domain going forward.

### Definition of Done
- HTTPS enforced, valid ACM certificate, HTTP requests redirect rather than serve.
- API key rotated post-TLS.
- `api.<domain>.com` resolves and is the canonical address used going forward (including in Goal 3's OpenClaw configuration).
- `terraform plan` shows no drift.

---

## Sequencing summary

| Goal | Depends on | Can start now? |
|---|---|---|
| 1. SQS worker deployment | Nothing external | Yes — in progress |
| 2. TLS/HTTPS | Domain registration clearing (external, ~24hr) | No — wait for confirmation |

## What NOT to do during this phase
- Do not run a real (non-synthetic) supplier file through the pipeline yet — that's a later step, and should happen only once TLS and the worker are both confirmed solid.
- Do not attempt a Keepa rate-limit load test yet — same reasoning.
- Do not let any of this phase's work touch business-rule logic, classification thresholds, or the approval workflow — this is infrastructure/connectivity only.
- Do not begin any OpenClaw-side implementation work yet — see below.

---

## Beyond this plan: OpenClaw

OpenClaw has not been installed, configured, or scoped at all yet. It stays on the roadmap as the milestone that proves the whole system works end-to-end, but it needs its own discovery pass before it can be turned into a concrete plan like this one — at minimum: how it's installed/hosted, its actual tool-calling/authentication mechanism, and what its base configuration looks like. That discovery should happen once Goals 1 and 2 above are done and the backend has a stable, secure, async-capable HTTPS endpoint worth pointing anything at.
