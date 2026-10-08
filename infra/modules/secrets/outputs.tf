output "sp_api_secret_arn" {
  value = aws_secretsmanager_secret.sp_api.arn
}

output "sp_api_secret_name" {
  value = aws_secretsmanager_secret.sp_api.name
}

output "keepa_secret_arn" {
  value = aws_secretsmanager_secret.keepa.arn
}

output "keepa_secret_name" {
  value = aws_secretsmanager_secret.keepa.name
}

output "api_key_secret_arn" {
  value = aws_secretsmanager_secret.api_key.arn
}

output "api_key_secret_name" {
  value = aws_secretsmanager_secret.api_key.name
}

output "openclaw_backend_token_secret_arn" {
  value = aws_secretsmanager_secret.openclaw_backend_token.arn
}

output "openclaw_backend_token_secret_name" {
  value = aws_secretsmanager_secret.openclaw_backend_token.name
}

output "telegram_reminder_secret_arn" {
  value = aws_secretsmanager_secret.telegram_reminder.arn
}

output "telegram_reminder_secret_name" {
  value = aws_secretsmanager_secret.telegram_reminder.name
}
