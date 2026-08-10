# Terraform state bootstrap

Run this **once**, manually, before touching `infra/`. It creates the S3
bucket and DynamoDB table that `infra/` uses as its remote state backend.
It deliberately keeps its own state local — remoting the state of "the
thing that creates remote state" isn't worth the complexity at this scale.

```bash
cd infra/bootstrap
terraform init
terraform apply -var="project_name=adc"
```

Note the outputs (`state_bucket_name`, `lock_table_name`) — they must match
the `bucket` / `dynamodb_table` values in `infra/backend.tf`.

Both the bucket and the lock table have `prevent_destroy` set. If you ever
need to tear this down, remove that lifecycle block first, deliberately.
