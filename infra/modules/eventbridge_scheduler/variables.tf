variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "cluster_arn" {
  type = string
}

variable "task_definition_arn" {
  type = string
}

variable "ecs_task_execution_role_arn" {
  description = "Passed to the launched task - the scheduler's own role needs iam:PassRole on this and task_role_arn."
  type        = string
}

variable "ecs_task_role_arn" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "ecs_security_group_id" {
  type = string
}

variable "state" {
  description = "ENABLED or DISABLED. Disabled 2026-10-10 for the v5 deploy (reminder_job.py has been erroring on every run since 2026-08-28 - see CLAUDE.md - and the stored telegram-reminder chat_id turned out to be the bot's own account, not a real person's). Set back to ENABLED only after both are actually fixed and reviewed, not just once the image is deployed."
  type        = string
  default     = "ENABLED"
}

variable "schedule_expression" {
  description = "EventBridge Scheduler rate/cron expression. 1 minute, per the locked approval state machine's reminder cadence (5-min initial wait, 10-min reminders - see docs/openclaw/OPENCLAW_APPROVAL_STATE_MACHINE_SPEC.md)."
  type        = string
  default     = "rate(1 minute)"
}
