variable "project_name" {
  description = "Short project slug used to prefix resource names (e.g. \"adc\")."
  type        = string
  default     = "adc"
}

variable "aws_region" {
  description = "AWS region for the state bucket / lock table."
  type        = string
  default     = "us-east-1"
}
