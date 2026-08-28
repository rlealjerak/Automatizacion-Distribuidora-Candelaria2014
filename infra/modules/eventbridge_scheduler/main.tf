############################################################
# EventBridge Scheduler -> ecs:RunTask, once a minute.
#
# Fires reminder_job.py as a fresh one-shot Fargate task each minute -
# see modules/ecs_cluster's reminder_job task definition (no service:
# this schedule is what actually runs it, ecs:RunTask each time, not a
# long-running process). Chosen over Lambda per
# docs/openclaw/BACKEND_CHANGES_FOR_OPENCLAW.md Section 5 - reuses the
# existing shared-image/task-role pattern instead of a second deploy
# pipeline.
############################################################

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project_name}-${var.environment}-reminder-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

# The scheduler's own role needs to both launch the task (ecs:RunTask)
# and hand off the two roles the launched task itself runs as
# (iam:PassRole on the execution + task roles) - same two-role split
# ecs_cluster's own task definitions use.
data "aws_iam_policy_document" "scheduler_run_task" {
  statement {
    sid       = "RunReminderJobTask"
    actions   = ["ecs:RunTask"]
    resources = [var.task_definition_arn]
    condition {
      test     = "ArnLike"
      variable = "ecs:cluster"
      values   = [var.cluster_arn]
    }
  }

  statement {
    sid       = "PassTaskRoles"
    actions   = ["iam:PassRole"]
    resources = [var.ecs_task_execution_role_arn, var.ecs_task_role_arn]
  }
}

resource "aws_iam_role_policy" "scheduler_run_task" {
  name   = "run-reminder-job-task"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_run_task.json
}

resource "aws_scheduler_schedule" "reminder_job" {
  name                = "${var.project_name}-${var.environment}-reminder-job"
  schedule_expression = var.schedule_expression

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = var.cluster_arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = var.task_definition_arn
      launch_type         = "FARGATE"
      task_count          = 1

      network_configuration {
        subnets          = var.public_subnet_ids
        security_groups  = [var.ecs_security_group_id]
        assign_public_ip = true # no NAT gateway - matches every other task's networking (see modules/network/main.tf)
      }
    }

    # A run that's still going when the next minute fires is left alone,
    # not queued/retried by the scheduler itself - reminder_job.py is a
    # fast single DB-read-then-maybe-one-Telegram-call script, overlap
    # isn't expected in practice, and a missed minute just means the next
    # one catches the same due reminder a minute later.
    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
