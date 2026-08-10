# Infra (Terraform)

Provisions: VPC (2 public + 2 private subnets, no NAT — see `modules/network`
for why), RDS PostgreSQL, S3 (supplier files), Secrets Manager (placeholder
secrets for SP-API/Keepa), SQS (+ DLQ), ECR, and an ECS cluster with IAM
roles. **No ECS service/task definition yet** — that's added once the
backend has a container image to deploy.

## First-time setup

```bash
# 1. One-time: create the remote state backend (see bootstrap/README.md)
cd infra/bootstrap
terraform init && terraform apply -var="project_name=adc"

# 2. Main infra
cd ../
terraform init
terraform plan
terraform apply
```

State locking is Terraform's native S3 lockfile support (`use_lockfile`,
Terraform >= 1.10) — no DynamoDB table, since the IAM user used to apply
this has no `dynamodb:*` permissions (confirmed via a real apply attempt).

Requires AWS CLI credentials configured locally (`aws configure` or an
SSO profile) with permission to create the resources above.

## After first apply

The SP-API and Keepa secrets are created with placeholder values. Populate
them for real, e.g.:

```bash
aws secretsmanager put-secret-value \
  --secret-id adc/prod/sp-api-credentials \
  --secret-string '{"refresh_token":"...","client_id":"...","client_secret":"...","role_arn":"..."}'

aws secretsmanager put-secret-value \
  --secret-id adc/prod/keepa-api-key \
  --secret-string '{"api_key":"..."}'
```

The RDS master password is auto-generated and rotated by AWS — retrieve it
via `module.rds.master_user_secret_arn` / the `rds_master_secret_arn`
output, never set it manually.

## IAM permissions needed to apply this

The `claude-code` IAM user's policy, as originally granted, covered S3,
Secrets Manager, and SQS only. Getting a full `terraform apply` to
succeed took three rounds of real `AccessDeniedException`s (EC2/VPC, IAM
role creation, ECS, ECR, CloudWatch Logs, and a couple of narrowly-scoped
gaps found on refresh - see docs/decisions/0003-infra-apply-findings.md
for the full narrative). `claude-code-iam-policy.json` in this directory
is the final, complete set of permissions that got the apply to succeed -
scoped to this project's resource name prefix (`adc-*`) and region
(`us-east-1`) where the AWS API supports it. Useful as a reference if
this ever needs reproducing (e.g. a second environment) - an account
admin attaches it, not `claude-code` itself (it has no
`iam:PutUserPolicy` on itself either).

## Cost note

Sized to stay well under the $500/mo total budget (infra + Keepa + SP-API)
at MVP scale: `db.t4g.micro` single-AZ, minimal Fargate task, no NAT
gateway, no ALB yet. Re-evaluate sizing once real traffic/data volume is
known — see `docs/decisions/0001-initial-architecture-decisions.md`.
