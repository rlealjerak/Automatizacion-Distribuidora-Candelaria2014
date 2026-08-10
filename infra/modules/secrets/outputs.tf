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
