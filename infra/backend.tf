# Remote state backend. Bucket/table must already exist - see
# infra/bootstrap/README.md. Values here must match that module's outputs.
#
# terraform init will prompt to migrate state the first time this is added;
# that's expected.
terraform {
  backend "s3" {
    bucket         = "adc-terraform-state"
    key            = "infra/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "adc-terraform-locks"
    encrypt        = true
  }
}
