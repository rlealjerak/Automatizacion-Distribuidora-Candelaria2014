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
  description = "Secrets Manager ARNs the task should be able to read (SP-API, Keepa, RDS master password)."
  type        = list(string)
}
