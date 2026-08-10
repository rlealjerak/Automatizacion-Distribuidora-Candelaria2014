output "endpoint" {
  value = aws_db_instance.main.endpoint
}

output "address" {
  value = aws_db_instance.main.address
}

output "port" {
  value = aws_db_instance.main.port
}

output "db_name" {
  value = aws_db_instance.main.db_name
}

output "master_user_secret_arn" {
  description = "Secrets Manager ARN holding the auto-managed master password."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}
