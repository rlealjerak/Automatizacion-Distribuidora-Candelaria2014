############################################################
# Application Load Balancer - the one inbound path into the VPC.
#
# HTTP only, no TLS. This project has no domain name / ACM certificate
# yet (would need a Route53 hosted zone + a domain, neither exists), so
# there is no way to terminate HTTPS here right now. That means the
# X-Api-Key header (see backend/src/adc_backend/modules/auth.py) travels
# in plaintext over the internet between the caller and this ALB.
# FLAGGED, not silently accepted: add a domain + ACM cert + HTTPS
# listener before this handles anything beyond development/testing
# traffic - same category as this project's other explicitly-flagged,
# not-yet-resolved gaps (RDS backup_retention_period, deletion_protection
# - see docs/decisions/0003-infra-apply-findings.md).
#
# Sits in the public subnets (same ones ECS Fargate tasks already use for
# outbound-only internet access - see modules/network/main.tf's NAT
# decision) and is the only thing allowed to reach the ECS tasks security
# group on the container port; see the security_group_rule in root
# main.tf.
############################################################

resource "aws_security_group" "alb" {
  name_prefix = "${var.project_name}-${var.environment}-alb-"
  description = "Public ALB - inbound HTTP from the internet, outbound to ECS tasks only."
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTP from anywhere - no TLS yet, see module docstring"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
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

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.backend.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }
}
