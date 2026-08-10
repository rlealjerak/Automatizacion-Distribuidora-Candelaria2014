# 0003 - Infra apply findings (first real `terraform apply` attempt)

Date: 2026-08-06, completed 2026-08-07

**Update 2026-08-07: `infra/` is now fully applied.** Every resource in
the plan exists in the real AWS account - VPC/networking, RDS, S3, both
Secrets Manager placeholder secrets, SQS+DLQ, ECR, ECS cluster, and both
IAM roles with their policies. `terraform plan` reports "No changes" (42
resources in state). What follows below is the narrative of getting there
across two more IAM permission rounds and one real account-level
constraint; kept as-is rather than rewritten, since the "why" for each
fix still matters going forward. See the final section for what's
actually live and what was found along the way.

## Context

First attempt to actually apply `infra/` against the real AWS account, now
that AWS CLI credentials (`claude-code` IAM user, account `617464676572`)
were configured. Two real problems surfaced immediately, both fixed or
documented rather than worked around silently.

## Findings

**`us-east-1` was never actually blocked for this account.** The first
bootstrap apply failed creating `adc-terraform-state` with an S3 error
claiming the region should be `eu-central-1`. Before treating that as an
account/org-level region restriction (which would have meant every region
reference across `infra/`, `.env.example`, and docs needed rewriting),
tested directly: created a throwaway, globally-unique-named bucket in
`us-east-1` from this same account - it succeeded immediately. Conclusion:
`adc-terraform-state` is just already taken by an unrelated AWS account
somewhere else (S3 bucket names are globally unique across every AWS
account, not just this one) - not a signal about this account at all.
Fixed by suffixing the bucket name with the account ID
(`adc-terraform-state-617464676572`) in `infra/bootstrap/main.tf`, the
standard fix for exactly this collision.

**Dropped DynamoDB state locking for S3-native locking.** The `claude-code`
IAM user has zero `dynamodb:*` permissions (confirmed via a real
`dynamodb:CreateTable` denial, not assumed). Rather than requesting that
permission be added, switched to Terraform's native S3 lockfile-based
locking (`use_lockfile = true`, needs Terraform >= 1.10 - this machine has
1.15.8). Removed the `aws_dynamodb_table` resource from
`infra/bootstrap/main.tf` entirely and the `dynamodb_table` line from
`infra/backend.tf`.

**`terraform apply` on the main `infra/` config partially succeeded, then
hit a hard IAM wall.** What's actually live in AWS as of this writing:
- S3 `adc-prod-supplier-files` bucket (versioned, encrypted, TLS-only
  policy, public access blocked)
- Secrets Manager `adc/prod/sp-api-credentials` and `adc/prod/keepa-api-key`
  (placeholder values only - still need real credentials via
  `aws secretsmanager put-secret-value`, see `infra/README.md`)
- SQS `adc-prod-list-processing` + `adc-prod-list-processing-dlq` with the
  redrive policy between them

What failed, all with `AccessDeniedException` / `UnauthorizedOperation`,
none attempted further because Terraform stopped applying dependents once
their dependencies failed: VPC/networking (`ec2:CreateVpc` and everything
else EC2), the two ECS IAM roles (`iam:CreateRole`), ECS cluster
(`ecs:CreateCluster`), the CloudWatch log group (`logs:CreateLogGroup`),
and ECR (`ecr:CreateRepository`). RDS was never reached at all - it
depends on the VPC's private subnets, which don't exist - so whether
`rds:CreateDBInstance` etc. are granted is still unknown, not confirmed
either way.

**Fix isn't something this session can do.** The `claude-code` user has no
`iam:PutUserPolicy`/`iam:AttachUserPolicy` on itself (checked - denied),
so it can't grant itself more access. Wrote
`infra/claude-code-iam-policy.json` - the additional EC2/RDS/ECR/ECS/Logs/
IAM permissions needed, scoped to this project's `adc-*` resource prefix
and `us-east-1` where the AWS API supports resource-level scoping for
those actions. An account admin needs to attach it, then `terraform plan`
/ `apply` can be re-run to pick up where it left off (existing S3/Secrets/
SQS resources stay untouched - Terraform only creates what's missing).

## Why this matters going forward

Confirms the project's own "don't block on AWS" decision (see CLAUDE.md
status notes) was the right call independent of this - even with working
credentials, the account's actual permission boundary wasn't knowable
until a real apply was attempted, and it's narrower than `infra/` assumes.
Steps 3 onward continue against local Docker Postgres / local parsing
logic; the AWS side of the system picks back up once the IAM policy is
widened and the rest of `infra/` applies cleanly.

## Completing the apply (2026-08-07)

Took two more rounds after the account admin first attached
`claude-code-iam-policy.json`, plus one real account-level constraint -
recorded here rather than folded silently into the file above, since each
was a genuine new finding, not a typo fix.

**Round 1** (after the first policy attach): VPC, subnets, route tables,
security groups, the ECS cluster, and both IAM roles all created
successfully. Three new things surfaced:
- `rds:CreateDBInstance` failed with `FreeTierRestrictionError: The
  specified backup retention period exceeds the maximum available to free
  tier customers` - **this AWS account is on some kind of free-tier or
  otherwise restricted plan**, not something previously known. AWS didn't
  report the actual allowed maximum, just that the hardcoded default (7
  days) exceeded it. Fixed by making `backup_retention_period` a variable
  (`infra/variables.tf`), defaulting to `0` (no automated backups) - the
  one value guaranteed to clear any tier's restriction. Flagged in
  `modules/rds/main.tf` as the same category of pre-launch gap as
  `deletion_protection`/`skip_final_snapshot`: fine while iterating,
  **must be revisited (raise it, and/or resolve whatever plan
  restriction caused this) before real data exists** with no backup
  recovery point otherwise.
- `ecr:ListTagsForResource` and a correctly-scoped `logs:DescribeLogGroups`
  were both missing from the policy - the ECR repo and CloudWatch log
  group had actually been created successfully, but the AWS Terraform
  provider's automatic post-create tag reconciliation (needed because the
  provider has a `default_tags` block - see `providers.tf`) failed
  without them, which Terraform reports as an apply error even though the
  underlying resource exists. `logs:DescribeLogGroups` specifically
  wouldn't work scoped to one log-group ARN - confirmed by a real
  `AccessDenied` on the scoped version, not assumed; it needed
  `Resource: "*"` since it's a list/search action, not one that operates
  on a single resource the way `CreateLogGroup`/`TagResource` do.

**Round 2** (after the second policy attach): a clean `terraform plan`
surfaced one more gap the same way - `ec2:DescribeSecurityGroupRules`,
needed to refresh the `aws_security_group_rule` resource created in round
1. Added, re-attached, and the final `terraform apply` completed clean:
6 resources added (the RDS instance, the ECS task role's two inline
policies, the ECR lifecycle policy), 2 destroyed-and-recreated (the ECR
repo and log group, both marked "tainted" by Terraform after round 1's
partial failures - confirmed empty/harmless to replace before applying,
since no container image or log data had ever been pushed to either).

**Final state:** `terraform plan` reports "No changes" against 42
tracked resources. Every AWS resource this project's `infra/` defines
now exists for real, in `us-east-1`, in the project's AWS account.
`infra/claude-code-iam-policy.json` in the repo reflects the final,
complete set of permissions that got this apply to succeed - useful as a
reference if this ever needs reproducing (e.g. a second environment).
