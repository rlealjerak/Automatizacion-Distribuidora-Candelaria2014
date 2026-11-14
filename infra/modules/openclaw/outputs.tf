output "ecr_repository_url" { value = aws_ecr_repository.openclaw.repository_url }
output "service_name" { value = aws_ecs_service.openclaw.name }
output "file_system_id" { value = aws_efs_file_system.state.id }
output "log_group_name" { value = aws_cloudwatch_log_group.openclaw.name }
