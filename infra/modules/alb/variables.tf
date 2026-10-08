variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "container_port" {
  type = number
}

variable "certificate_arn" {
  description = "Validated ACM certificate ARN. Empty string = HTTP-only bootstrap mode (see main.tf docstring)."
  type        = string
  default     = ""
}
