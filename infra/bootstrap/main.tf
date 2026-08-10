############################################################
# Bootstrap: creates the S3 bucket that the *real* infra (../)
# uses as its Terraform remote state backend.
#
# This is a chicken-and-egg problem: Terraform state needs a
# place to live before any other resource exists. This module
# is applied ONCE, manually, with local state (its own state
# file is small and not worth remoting). Do not add unrelated
# resources here.
#
# State locking uses Terraform's native S3 lockfile support
# (`use_lockfile = true` in ../backend.tf, Terraform >= 1.10) -
# no DynamoDB table. Originally designed with a DynamoDB lock
# table, but the `claude-code` IAM user has no dynamodb:* grants
# at all (confirmed via a real apply attempt, not assumed), and
# adding that permission was avoidable rather than necessary.
#
# Usage:
#   cd infra/bootstrap
#   terraform init
#   terraform apply -var="project_name=adc"
############################################################

terraform {
  required_version = ">= 1.10.0" # use_lockfile (S3-native state locking) needs >= 1.10
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

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "tf_state" {
  # S3 bucket names are globally unique across *every* AWS account, not just
  # this one - "${project_name}-terraform-state" collided with an unrelated
  # account's existing bucket on first apply (confirmed via a real
  # CreateBucket call, not assumed - it returned a region-mismatch error
  # pointing at a region this account doesn't actually use). Account-ID
  # suffix makes collision effectively impossible going forward.
  bucket = "${var.project_name}-terraform-state-${data.aws_caller_identity.current.account_id}"

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
