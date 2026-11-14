variable "project_name" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string }

variable "vpc_id" { type = string }
variable "public_subnet_ids" {
  description = "Where the task runs - no NAT gateway exists (modules/network), so outbound HTTPS needs a public IP, same as the backend/worker."
  type        = list(string)
}
variable "private_subnet_ids" {
  description = "Where the EFS mount targets live (one per AZ) - reachable from the public subnets in the same AZ over the VPC's local route."
  type        = list(string)
}

variable "cluster_arn" { type = string }
variable "image_tag" {
  description = "Tag in this module's own ECR repo - explicit versions (oc1, oc2, ...), same immutable-tag pattern as the backend image."
  type        = string
}

variable "task_cpu" {
  type    = number
  default = 512 # OPENCLAW_DEPLOYMENT_PLAN.md Section 3's starting point
}
variable "task_memory" {
  type    = number
  default = 1024
}

variable "anthropic_secret_arn" { type = string }     # {"api_key": ...}
variable "gateway_token_secret_arn" { type = string } # {"token": ...}
variable "telegram_secret_arn" { type = string }      # {"bot_token", "chat_id"} - shares the reminder job's bot
variable "backend_token_secret_arn" { type = string } # {"token": ...} - what /mcp checks
