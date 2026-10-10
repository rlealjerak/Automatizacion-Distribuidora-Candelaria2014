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

# Static bearer token OpenClaw's MCP client sends to /mcp - a separate
# credential from api_key above (different surface, different caller
# expectation). See backend/src/adc_backend/modules/mcp_server/auth.py
# and docs/openclaw/BACKEND_CHANGES_FOR_OPENCLAW.md Section 6.
resource "aws_secretsmanager_secret" "openclaw_backend_token" {
  name        = "${var.project_name}/${var.environment}/openclaw-backend-token"
  description = "Static bearer token OpenClaw's MCP client sends to /mcp. Populated manually after apply."
}

resource "aws_secretsmanager_secret_version" "openclaw_backend_token" {
  secret_id     = aws_secretsmanager_secret.openclaw_backend_token.id
  secret_string = jsonencode({ token = "replace-me-via-console-or-cli" })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# Bot token + target chat id for reminder_job.py's direct Telegram calls -
# see backend/src/adc_backend/telegram_notifier.py for why this backend
# talks to Telegram directly at all (OpenClaw takes no inbound traffic,
# per docs/openclaw/OPENCLAW_DEPLOYMENT_PLAN.md Section 4, so it can't be
# asked to send the reminder itself). chat_id can only be populated once
# the owner has paired with OpenClaw's bot - a real sequencing dependency
# on that separate deployment track, not a blocker for provisioning the
# secret container itself now.
resource "aws_secretsmanager_secret" "telegram_reminder" {
  name        = "${var.project_name}/${var.environment}/telegram-reminder"
  description = "Telegram bot token + chat id for the approval reminder job. Populated manually after apply, once OpenClaw's bot is paired."
}

resource "aws_secretsmanager_secret_version" "telegram_reminder" {
  secret_id     = aws_secretsmanager_secret.telegram_reminder.id
  secret_string = jsonencode({ bot_token = "replace-me-via-console-or-cli", chat_id = "replace-me-via-console-or-cli" })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# OpenClaw's own model access (not this backend's) - a frontier-tier
# Claude model is configured deliberately, not a cheaper one, since
# OpenClaw holds this backend's bearer token and can call
# approve_decision/revoke_decision (see openclaw/README.md and
# docs/openclaw/OPENCLAW_DEPLOYMENT_PLAN.md Section 0b). Same
# {"api_key": ...} shape as the Keepa secret above, for consistency -
# no ECS task consumes this yet, since OpenClaw's own infrastructure
# (EFS/task definition/service) doesn't exist in this repo yet either.
resource "aws_secretsmanager_secret" "openclaw_anthropic_key" {
  name        = "${var.project_name}/${var.environment}/openclaw-anthropic-key"
  description = "Anthropic API key for OpenClaw's own agent (not this backend's). Populated manually after apply."
}

resource "aws_secretsmanager_secret_version" "openclaw_anthropic_key" {
  secret_id     = aws_secretsmanager_secret.openclaw_anthropic_key.id
  secret_string = jsonencode({ api_key = "replace-me-via-console-or-cli" })

  lifecycle {
    ignore_changes = [secret_string]
  }
}
