############################################################
# ECS cluster + IAM roles + log group + task definition + service.
#
# Task definition/service added 2026-08-15, once a real image existed in
# ECR to deploy (see backend/Dockerfile, entrypoint.sh) - this module's
# own comment previously flagged this as the deferred follow-up step.
############################################################

resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-${var.environment}-cluster"

  setting {
    name  = "containerInsights"
    value = "disabled" # extra CloudWatch cost; enable later if debugging needs it
  }
}

resource "aws_cloudwatch_log_group" "backend" {
  name              = "/ecs/${var.project_name}-${var.environment}-backend"
  retention_in_days = 30
}

# --- Task execution role: what ECS itself needs (pull image, write logs, read secrets for env injection) ---

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_task_execution" {
  name               = "${var.project_name}-${var.environment}-ecs-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_task_execution_secrets" {
  statement {
    sid       = "ReadSecretsForTaskDefinition"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = var.secret_arns
  }
}

resource "aws_iam_role_policy" "ecs_task_execution_secrets" {
  name   = "read-secrets"
  role   = aws_iam_role.ecs_task_execution.id
  policy = data.aws_iam_policy_document.ecs_task_execution_secrets.json
}

# --- Task role: what the application code itself needs at runtime ---

resource "aws_iam_role" "ecs_task" {
  name               = "${var.project_name}-${var.environment}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "ecs_task_app_permissions" {
  statement {
    sid     = "SupplierFilesBucket"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [
      var.s3_bucket_arn,
      "${var.s3_bucket_arn}/*",
    ]
  }

  statement {
    sid       = "ListProcessingQueue"
    actions   = ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [var.sqs_queue_arn]
  }

  statement {
    sid       = "ReadAppSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = var.secret_arns
  }
}

resource "aws_iam_role_policy" "ecs_task_app_permissions" {
  name   = "app-permissions"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task_app_permissions.json
}

# --- Task definition + service: the API, behind the ALB ---
#
# Only non-secret config goes in `environment` below - every secret VALUE
# is fetched by the app itself at runtime via boto3 (config.get_secret),
# never injected by ECS. That's why there's no `secrets` block in the
# container definition even though the task role can read them - the app
# needs the secret *names* as plain env vars, not the values.
#
# Shared with the worker task definition below it - same image, same
# config surface (the worker uses the same Settings class), just a
# different container `command`. Keeping one env-var list means the two
# task definitions can't drift out of sync.

locals {
  backend_environment = [
    { name = "ENVIRONMENT", value = var.environment },
    { name = "AWS_REGION", value = var.aws_region },
    { name = "S3_BUCKET_NAME", value = var.s3_bucket_name },
    { name = "SQS_QUEUE_URL", value = var.sqs_queue_url },
    { name = "SP_API_SECRET_NAME", value = var.sp_api_secret_name },
    { name = "KEEPA_SECRET_NAME", value = var.keepa_secret_name },
    { name = "DB_SECRET_NAME", value = var.db_secret_name },
    { name = "API_KEY_SECRET_NAME", value = var.api_key_secret_name },
    { name = "SP_API_SELLER_ID", value = var.sp_api_seller_id },
    { name = "DB_HOST", value = var.db_host },
    { name = "DB_PORT", value = tostring(var.db_port) },
    { name = "DB_NAME", value = var.db_name },
    { name = "DB_USERNAME", value = var.db_username },
  ]
}

resource "aws_ecs_task_definition" "backend" {
  family                   = "${var.project_name}-${var.environment}-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "backend"
      image     = var.container_image
      essential = true
      portMappings = [
        { containerPort = var.container_port, protocol = "tcp" }
      ]
      environment = local.backend_environment
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.backend.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "backend"
        }
      }
    }
  ])

  tags = {
    Name = "${var.project_name}-${var.environment}-backend-task"
  }
}

resource "aws_ecs_service" "backend" {
  name            = "${var.project_name}-${var.environment}-backend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.backend.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [var.ecs_security_group_id]
    assign_public_ip = true # no NAT gateway - see modules/network/main.tf
  }

  load_balancer {
    target_group_arn = var.target_group_arn
    container_name   = "backend"
    container_port   = var.container_port
  }

  # entrypoint.sh runs migrations + the idempotent rules-config seed
  # before the app starts listening - give the health check enough grace
  # period to not flap a task that's still migrating a fresh/behind DB.
  health_check_grace_period_seconds = 60

  depends_on = [aws_iam_role_policy_attachment.ecs_task_execution_managed]
}

# --- Task definition + service: the SQS worker (background list processing) ---
#
# Added once the async processing path existed to enqueue to
# (POST /runs/{run_id}/process now pushes to SQS instead of calling
# process_run inline - see modules/tools/router.py). Shares the API's
# image (same task role already grants sqs:ReceiveMessage/DeleteMessage/
# SendMessage/GetQueueAttributes on this queue - see ecs_task_app_permissions
# above, provisioned in anticipation of this) but overrides `command` to
# skip entrypoint.sh (migrations/seed - the API service already runs
# those on every start) and run worker.py's long-poll loop directly.
# No load balancer attachment - this service takes no inbound traffic.

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${var.project_name}-${var.environment}-worker"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.project_name}-${var.environment}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name        = "worker"
      image       = var.container_image
      essential   = true
      command     = ["python", "-m", "adc_backend.worker"]
      environment = local.backend_environment
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.worker.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "worker"
        }
      }
    }
  ])

  tags = {
    Name = "${var.project_name}-${var.environment}-worker-task"
  }
}

resource "aws_ecs_service" "worker" {
  name            = "${var.project_name}-${var.environment}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = 1 # not autoscaled yet - future optimization once real load is observed
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [var.ecs_security_group_id]
    assign_public_ip = true # no NAT gateway - see modules/network/main.tf
  }

  depends_on = [aws_iam_role_policy_attachment.ecs_task_execution_managed]
}
