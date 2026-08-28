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

variable "schedule_expression" {
  description = "EventBridge Scheduler rate/cron expression. 1 minute, per the locked approval state machine's reminder cadence (5-min initial wait, 10-min reminders - see docs/openclaw/OPENCLAW_APPROVAL_STATE_MACHINE_SPEC.md)."
  type        = string
  default     = "rate(1 minute)"
}
