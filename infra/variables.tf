variable "project_name" {
  description = "Short project slug used to prefix resource names."
  type        = string
  default     = "adc"
}

variable "environment" {
  description = "Deployment environment name. MVP runs a single environment; revisit if/when a staging env is needed."
  type        = string
  default     = "prod"
}

variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "AZs to spread subnets across. Two is enough for MVP durability without paying for a third NAT/subnet set we don't use."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

# --- RDS ---

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro is the smallest Graviton burstable class - fine for MVP row volumes; resize if query load grows."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "RDS allocated storage in GB."
  type        = number
  default     = 20
}

variable "db_name" {
  description = "Default database name."
  type        = string
  default     = "adc"
}

variable "db_username" {
  description = "Master username. Password is auto-managed by RDS in Secrets Manager (manage_master_user_password) - never set a password here."
  type        = string
  default     = "adc_admin"
}

variable "db_deletion_protection" {
  description = "True since 2026-10 - real supplier/pricing data now lives here (approval queue, classification history) that nobody has a recovery path for otherwise."
  type        = bool
  default     = true
}

variable "db_skip_final_snapshot" {
  description = "Mirrors db_deletion_protection - flip to false once this is handling real supplier lists."
  type        = bool
  default     = true
}

variable "db_backup_retention_period" {
  description = "Days of automated RDS backups. Previously 0 because this AWS account's plan rejected any nonzero value (FreeTierRestrictionError, confirmed via a real apply) - retrying at 7 now; revert to 0 if the same error recurs on apply."
  type        = number
  default     = 7
}

# --- SQS ---

variable "sqs_visibility_timeout_seconds" {
  description = "Must exceed the worst-case time to process one queue message. Messages represent one list-run job (not one row), so this needs to cover a full run's worth of per-row SP-API/Keepa calls."
  type        = number
  default     = 1800 # 30 min - revisit once real per-row timing is measured; a >5000-row run may need this higher or a heartbeat/extend-visibility pattern.
}

variable "sqs_max_receive_count" {
  description = "Times a message is retried before moving to the dead-letter queue."
  type        = number
  default     = 3
}

# --- ECS ---

variable "ecs_task_cpu" {
  description = "Fargate task CPU units (256 = .25 vCPU). Starting small given budget; the worker does I/O-bound API calls, not compute."
  type        = number
  default     = 512
}

variable "ecs_task_memory" {
  description = "Fargate task memory in MB."
  type        = number
  default     = 1024
}

variable "container_port" {
  description = "Port the backend container listens on."
  type        = number
  default     = 8000
}

variable "container_image_tag" {
  description = "ECR image tag to deploy. The repo has immutable tags (see modules/ecr), so 'latest' can never actually be pushed twice - found live on the first real deploy (2026-08-16). Using explicit version tags (v1, v2, ...) for now; a real CI/deploy pipeline would use git-SHA tags instead, flagged not fixed for now."
  type        = string
  default     = "v4"
}

variable "sp_api_seller_id" {
  description = "This project's own Amazon Selling Partner ID (merchant token) - non-secret, just an account identifier (see backend config.py). Confirmed live 2026-08-15 via a real SP-API fees-estimate response echoing it back."
  type        = string
  default     = "AGKTO8HZBBQC4"
}

# --- DNS / TLS (Goal 2) ---

variable "domain_name" {
  description = "Apex domain for this project, registered at Namecheap (not Route53 - see modules/dns). Route53 only hosts DNS for it after nameservers are manually pointed there."
  type        = string
  default     = "distribuidoracandelaria2014ops.com"
}

variable "backend_subdomain" {
  description = "Subdomain label the backend API is served at, e.g. \"api\" for api.<domain_name>."
  type        = string
  default     = "api"
}
