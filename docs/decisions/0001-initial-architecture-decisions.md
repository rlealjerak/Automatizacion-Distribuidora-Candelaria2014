# 0001 - Initial architecture decisions (build-order step 1)

Date: 2026-08-04

## Context

Starting the MVP from an empty repo. A handful of decisions had to be made
before any infra or app code could be written. Recorded here so the
reasoning isn't lost, per the project's "flag decisions that are hard to
reverse later" working rule.

## Decisions

**IaC: Terraform.** Chosen over CDK/SAM for portability and because it's
the most common choice a future engineer would already know. Reversible,
but a rewrite - not a config change.

**Backend language: Python.** Chosen over Node for the parsing/matching-
heavy workload (PDF extraction, fuzzy text/brand matching, pandas-style
data wrangling) - this is where most of the domain-specific complexity
lives, more so than in the API/orchestration layer.

**Compute: ECS Fargate**, not Lambda. A 5,000+ row run making live
SP-API/Keepa calls per row can run well past Lambda's 15-minute limit;
chunking around that adds real complexity for no benefit at this scale.
Not EC2, to avoid owning OS patching/process supervision for a two-person
team. Two logical services will eventually exist on one cluster: the API
(OpenClaw-facing) and the SQS worker (list processing) - only the cluster
and IAM roles exist so far; task definitions/services are added once
there's a container image to run.

**Networking: no NAT gateway.** ECS Fargate tasks run in public subnets
with a security group that has no inbound rules, giving them outbound
internet access (required for external SP-API/Keepa calls) without a NAT
gateway's ~$32-70/mo cost. RDS stays in private subnets with no internet
route, reachable only from the ECS security group. **Hard to reverse
later** in the sense that adding an ALB + moving Fargate to private
subnets is a real (if mechanical) migration, not a flag flip - worth
doing if/when inbound exposure becomes a concern (e.g. OpenClaw calling
this backend's API directly over the internet instead of via some other
integration path). Flagging now so it's a deliberate choice, not an
oversight.

**RDS credentials: AWS-managed master password** (`manage_master_user_password`),
not a Terraform-set password. The master password is generated and rotated
by AWS directly into Secrets Manager - it never touches Terraform state,
tfvars, or any file in this repo.

**Single environment for now** (`prod`), not dev/staging/prod. Team size
(one owner, one developer) doesn't justify multi-environment infra yet.
Revisit if a second environment becomes worth the cost/complexity -
the module structure under `infra/modules/` doesn't need to change,
only the root config would need to be parameterized per environment.

**RDS deletion protection: off, skip-final-snapshot: on, for now**
(`db_deletion_protection` / `db_skip_final_snapshot` in `infra/variables.tf`).
Deliberately easy to tear down and rebuild while the schema is still
being iterated on (build-order step 2 hasn't landed yet). **Must be
flipped before this holds any real supplier/pricing data anyone would be
upset to lose** - flagging here so it isn't forgotten once step 2 lands.

**SQS message granularity: one message per list-run**, not one message
per row. Per-row error isolation happens inside the worker (against
Postgres-tracked row state), not via SQS. This keeps the queue simple but
means visibility timeout has to cover a full run's processing time - the
default (30 min) is a placeholder until real per-row timing against
SP-API/Keepa is measured; may need to move to a heartbeat/extend-visibility
pattern for very large runs.

**No task definition/ECS service yet.** Provisioned everything that
doesn't depend on the app existing (VPC, RDS, S3, Secrets Manager, SQS,
ECR, ECS cluster + IAM roles). The actual task definition + service comes
once there's a container image worth deploying - bundling it into this
increment would mean deploying an app that's still just a health check.
