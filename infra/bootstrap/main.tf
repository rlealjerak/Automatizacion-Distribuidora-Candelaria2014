############################################################
# Bootstrap: creates the S3 bucket + DynamoDB lock table that
# the *real* infra (../) uses as its Terraform remote state
# backend.
#
# This is a chicken-and-egg problem: Terraform state needs a
# place to live before any other resource exists. This module
# is applied ONCE, manually, with local state (its own state
# file is small and not worth remoting). Do not add unrelated
# resources here.
#
# Usage:
#   cd infra/bootstrap
#   terraform init
#   terraform apply -var="project_name=adc"
############################################################

terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  # Intentionally local state - see header comment.
}

provider "aws" {
  region = var.aws_region
}

resource "aws_s3_bucket" "tf_state" {
  bucket = "${var.project_name}-terraform-state"

  # Safety net: prevents `terraform destroy` from ever deleting
  # the bucket holding every other environment's state.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tf_state" {
  bucket                  = aws_s3_bucket.tf_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "tf_lock" {
  name         = "${var.project_name}-terraform-locks"
  billing_mode = "PAY_PER_REQUEST" # trivial cost at this scale, no capacity planning needed
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  lifecycle {
    prevent_destroy = true
  }
}
