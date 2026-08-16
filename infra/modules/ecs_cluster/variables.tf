variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "s3_bucket_arn" {
  type = string
}

variable "sqs_queue_arn" {
  type = string
}

variable "secret_arns" {
  description = "Secrets Manager ARNs the task should be able to read (SP-API, Keepa, RDS master password, API key)."
  type        = list(string)
}

# --- task definition + service ---

variable "container_image" {
  description = "Full ECR image URI, including tag (e.g. <repo_url>:<tag>)."
  type        = string
}

variable "container_port" {
  type = number
}

variable "task_cpu" {
  type = number
}

variable "task_memory" {
  type = number
}

variable "aws_region" {
  type = string
}

variable "desired_count" {
  description = "Number of running tasks. 1 for MVP - no HA requirement yet, and it keeps Fargate cost minimal."
  type        = number
  default     = 1
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "ecs_security_group_id" {
  type = string
}

variable "target_group_arn" {
  description = "ALB target group to register tasks with."
  type        = string
}

# Non-secret container env vars - see main.tf's comment on why these are
# plain env vars (names/identifiers) rather than ECS-injected secrets.
variable "s3_bucket_name" {
  type = string
}

variable "sqs_queue_url" {
  type = string
}

variable "sp_api_secret_name" {
  type = string
}

variable "keepa_secret_name" {
  type = string
}

variable "db_secret_name" {
  type = string
}

variable "api_key_secret_name" {
  type = string
}

variable "sp_api_seller_id" {
  type = string
}

variable "db_host" {
  type = string
}

variable "db_port" {
  type = number
}

variable "db_name" {
  type = string
}

variable "db_username" {
  type = string
}
