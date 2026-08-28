output "schedule_arn" {
  value = aws_scheduler_schedule.reminder_job.arn
}

output "scheduler_role_arn" {
  value = aws_iam_role.scheduler.arn
}
