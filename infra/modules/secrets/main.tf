############################################################
# Secrets Manager
#
# Terraform creates the secret *containers* with placeholder
# values only. Real SP-API and Keepa credentials must be entered
# manually (console or `aws secretsmanager put-secret-value`)
# after apply - they must never be committed, put in tfvars, or
# appear in Terraform state as a real value.
#
# `ignore_changes` on secret_string means terraform apply will
# never overwrite a value you've set manually.
############################################################

resource "aws_secretsmanager_secret" "sp_api" {
  name        = "${var.project_name}/${var.environment}/sp-api-credentials"
  description = "Amazon SP-API credentials (refresh token, client id/secret, IAM role ARN). Populated manually after apply."
}

resource "aws_secretsmanager_secret_version" "sp_api" {
  secret_id     = aws_secretsmanager_secret.sp_api.id
  secret_string = jsonencode({ placeholder = "replace-me-via-console-or-cli" })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "keepa" {
  name        = "${var.project_name}/${var.environment}/keepa-api-key"
  description = "Keepa API key. Populated manually after apply."
}

resource "aws_secretsmanager_secret_version" "keepa" {
  secret_id     = aws_secretsmanager_secret.keepa.id
  secret_string = jsonencode({ placeholder = "replace-me-via-console-or-cli" })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# Shared key every caller (OpenClaw) must send as X-Api-Key once this
# backend sits behind a public ALB - see backend/src/adc_backend/
# modules/auth.py. Same placeholder-then-populate-by-hand pattern as the
# two secrets above.
resource "aws_secretsmanager_secret" "api_key" {
  name        = "${var.project_name}/${var.environment}/api-key"
  description = "Shared API key for the OpenClaw-facing HTTP API (X-Api-Key header). Populated manually after apply."
}

resource "aws_secretsmanager_secret_version" "api_key" {
  secret_id     = aws_secretsmanager_secret.api_key.id
  secret_string = jsonencode({ placeholder = "replace-me-via-console-or-cli" })

  lifecycle {
    ignore_changes = [secret_string]
  }
}
