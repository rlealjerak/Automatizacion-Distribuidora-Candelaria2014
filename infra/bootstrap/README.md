# Terraform state bootstrap

Run this **once**, manually, before touching `infra/`. It creates the S3
bucket that `infra/` uses as its remote state backend, with native S3
lockfile-based locking (`use_lockfile = true`, Terraform >= 1.10) — no
DynamoDB table. It deliberately keeps its own state local — remoting the
state of "the thing that creates remote state" isn't worth the complexity
at this scale.

```bash
cd infra/bootstrap
terraform init
terraform apply -var="project_name=adc"
```

Note the output (`state_bucket_name`) — it must match the `bucket` value in
`infra/backend.tf`. The bucket name is suffixed with the AWS account ID
(`adc-terraform-state-<account-id>`) because S3 bucket names are globally
unique across *every* AWS account, not just this one — the unsuffixed name
collided with an unrelated account's existing bucket on first attempt.

The bucket has `prevent_destroy` set. If you ever need to tear this down,
remove that lifecycle block first, deliberately.
