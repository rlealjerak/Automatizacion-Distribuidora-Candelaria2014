# Remote state backend. Bucket must already exist - see
# infra/bootstrap/README.md. Value here must match that module's output.
# Hardcoded (not a variable) because Terraform backend blocks can't
# reference variables/data sources - they're resolved before anything else.
# Account ID 617464676572 is this project's AWS account, baked into the
# bucket name by bootstrap specifically to guarantee global uniqueness.
#
# use_lockfile = true is Terraform's native S3-based state locking
# (>= 1.10) - no DynamoDB table. See bootstrap/main.tf for why (the
# claude-code IAM user has no dynamodb:* permissions at all).
#
# terraform init will prompt to migrate state the first time this is added;
# that's expected.
terraform {
  backend "s3" {
    bucket       = "adc-terraform-state-617464676572"
    key          = "infra/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
