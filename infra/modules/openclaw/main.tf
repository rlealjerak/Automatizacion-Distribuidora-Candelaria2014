############################################################
# OpenClaw (Telegram approval bot) - its own image, its own task/service,
# its own least-privilege IAM. Added 2026-11-14, implementing
# docs/openclaw/OPENCLAW_DEPLOYMENT_PLAN.md Sections 1-6 / checklist 2-5.
#
# Architecture notes (deliberate, see the deployment plan):
#  - No ALB, no inbound rules at all: the Gateway binds to loopback and
#    only makes outbound calls (Telegram long-polling, Anthropic, this
#    project's own /mcp over HTTPS).
#  - Persistent state (pairing allowlist, sqlite runtime state, sessions)
#    lives on EFS at /home/node/.openclaw - Fargate disk is ephemeral and
#    losing it would force re-pairing on every deploy.
#  - Exactly one task, ever: two Gateways long-polling the same Telegram
#    bot token fight over getUpdates (Telegram returns 409 Conflict), so
#    deployments stop the old task before starting the new one.
#  - Config is baked into the image from openclaw/config/openclaw.json5
#    and copied onto the volume at every start (see openclaw/Dockerfile),
#    so git stays the single source of truth.
############################################################

locals {
  name = "${var.project_name}-${var.environment}-openclaw"
}

resource "aws_ecr_repository" "openclaw" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "openclaw" {
  repository = aws_ecr_repository.openclaw.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "openclaw" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
}

# --- Networking: outbound only ---

resource "aws_security_group" "openclaw" {
  name        = "${local.name}-task"
  description = "OpenClaw Gateway task - no inbound; outbound HTTPS + NFS to its EFS only"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "openclaw_https" {
  security_group_id = aws_security_group.openclaw.id
  description       = "Telegram, Anthropic, this backend's /mcp"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "openclaw_nfs" {
  security_group_id            = aws_security_group.openclaw.id
  description                  = "EFS state volume"
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
  referenced_security_group_id = aws_security_group.efs.id
}

resource "aws_security_group" "efs" {
  name        = "${local.name}-efs"
  description = "OpenClaw state EFS - NFS from the OpenClaw task only"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "efs_from_openclaw" {
  security_group_id            = aws_security_group.efs.id
  description                  = "NFS from the OpenClaw task"
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
  referenced_security_group_id = aws_security_group.openclaw.id
}

# --- Persistent state ---

resource "aws_efs_file_system" "state" {
  creation_token = "${local.name}-state"
  encrypted      = true

  tags = {
    Name = "${local.name}-state"
  }
}

resource "aws_efs_mount_target" "state" {
  count           = length(var.private_subnet_ids)
  file_system_id  = aws_efs_file_system.state.id
  subnet_id       = var.private_subnet_ids[count.index]
  security_groups = [aws_security_group.efs.id]
}

# uid/gid 1000 = the image's `node` user (confirmed via `docker exec ... id`,
# 2026-11-14). The access point forces that identity and creates the root
# directory owned by it, so the container never needs root on the volume.
resource "aws_efs_access_point" "state" {
  file_system_id = aws_efs_file_system.state.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/openclaw"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0700"
    }
  }
}

# --- IAM: separate roles from the backend's - OpenClaw gets none of S3/SQS/RDS ---

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid     = "ReadOpenClawSecretsForInjection"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      var.anthropic_secret_arn,
      var.gateway_token_secret_arn,
      var.telegram_secret_arn,
      var.backend_token_secret_arn,
    ]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-openclaw-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "MountStateVolumeViaAccessPoint"
    actions   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
    resources = [aws_efs_file_system.state.arn]
    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.state.arn]
    }
  }

  # ECS Exec - the only way into this task (no SSH, no public port), e.g.
  # `openclaw pairing approve telegram <CODE>` per the deployment plan's
  # checklist step 8.
  statement {
    sid = "EcsExec"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "openclaw-runtime"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# --- Task + service ---

resource "aws_ecs_task_definition" "openclaw" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "openclaw-state"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.state.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.state.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([
    {
      name      = "openclaw"
      image     = "${aws_ecr_repository.openclaw.repository_url}:${var.image_tag}"
      essential = true
      # Names match the ${VAR} references in openclaw/config/openclaw.json5.
      # `<arn>:<json-key>::` pulls one key out of a JSON secret.
      secrets = [
        { name = "ANTHROPIC_API_KEY", valueFrom = "${var.anthropic_secret_arn}:api_key::" },
        { name = "OPENCLAW_GATEWAY_TOKEN", valueFrom = "${var.gateway_token_secret_arn}:token::" },
        { name = "TELEGRAM_BOT_TOKEN", valueFrom = "${var.telegram_secret_arn}:bot_token::" },
        { name = "CANDELARIA_BACKEND_TOKEN", valueFrom = "${var.backend_token_secret_arn}:token::" },
      ]
      mountPoints = [
        { sourceVolume = "openclaw-state", containerPath = "/home/node/.openclaw", readOnly = false }
      ]
      # Verified locally 2026-10-09 and again 2026-11-14 against this exact image.
      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://127.0.0.1:18789/healthz || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 240 # doctor --fix + plugin/MCP startup took ~126s locally under amd64 emulation
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.openclaw.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "openclaw"
        }
      }
    }
  ])

  tags = {
    Name = "${local.name}-task"
  }
}

resource "aws_ecs_service" "openclaw" {
  name                   = local.name
  cluster                = var.cluster_arn
  task_definition        = aws_ecs_task_definition.openclaw.arn
  desired_count          = 1
  launch_type            = "FARGATE"
  platform_version       = "1.4.0" # EFS volumes need >= 1.4.0
  enable_execute_command = true

  # Stop-then-start, never two at once (see the module docstring on
  # Telegram getUpdates conflicts). Means a few seconds of bot downtime
  # per deploy - acceptable for an approval bot, unlike a duplicate.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [aws_security_group.openclaw.id]
    assign_public_ip = true # no NAT gateway - see modules/network/main.tf
  }

  depends_on = [aws_efs_mount_target.state]
}
