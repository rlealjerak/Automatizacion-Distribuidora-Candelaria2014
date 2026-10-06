############################################################
# Application Load Balancer - the one inbound path into the VPC.
#
# `var.certificate_arn` gates HTTPS: empty during the bootstrap phase
# before a domain + validated ACM cert exist (HTTP-only, X-Api-Key
# travels in plaintext - was the permanent state until Goal 2/TLS
# landed, see docs/decisions/0003-infra-apply-findings.md), non-empty
# once modules/dns has a validated cert. With a cert, port 443 opens,
# the HTTPS listener forwards to the backend, and the HTTP listener
# switches from forwarding to a 301 redirect onto HTTPS - never both
# forwarding and redirecting at once.
#
# Sits in the public subnets (same ones ECS Fargate tasks already use for
# outbound-only internet access - see modules/network/main.tf's NAT
# decision) and is the only thing allowed to reach the ECS tasks security
# group on the container port; see the security_group_rule in root
# main.tf.
############################################################

resource "aws_security_group" "alb" {
  name_prefix = "${var.project_name}-${var.environment}-alb-"
  # NOTE: description is immutable on a security group once created -
  # changing this string would force a full SG replacement (cascading
  # into the cross-module aws_security_group_rule.ecs_from_alb in root
  # main.tf). Left as its original wording for that reason, even though
  # "HTTP only" is no longer quite accurate once var.certificate_arn is set.
  description = "Public ALB - inbound HTTP from the internet, outbound to ECS tasks only."
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTP from anywhere - redirects to HTTPS once var.certificate_arn is set"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  dynamic "ingress" {
    for_each = var.certificate_arn != "" ? [1] : []
    content {
      description = "HTTPS from anywhere"
      from_port   = 443
      to_port     = 443
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  egress {
    description = "To ECS tasks only"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-alb-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb" "backend" {
  name               = "${var.project_name}-${var.environment}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  # No deletion_protection - matches this project's current stance on RDS
  # (easy to tear down/rebuild while iterating on MVP infra).
  enable_deletion_protection = false

  tags = {
    Name = "${var.project_name}-${var.environment}-alb"
  }
}

resource "aws_lb_target_group" "backend" {
  name        = "${var.project_name}-${var.environment}-backend-tg"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip" # Fargate awsvpc mode - targets are ENIs, not instances

  health_check {
    path                = "/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 5
    matcher             = "200"
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-backend-tg"
  }
}

# Two mutually-exclusive listeners on port 80, toggled by count - not one
# listener with a ternary `type`, because aws_lb_listener rejects
# target_group_arn being present (even set to null via an expression)
# alongside type="redirect".
resource "aws_lb_listener" "http_forward" {
  count = var.certificate_arn == "" ? 1 : 0

  load_balancer_arn = aws_lb.backend.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  count = var.certificate_arn != "" ? 1 : 0

  load_balancer_arn = aws_lb.backend.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  count = var.certificate_arn != "" ? 1 : 0

  load_balancer_arn = aws_lb.backend.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }
}
