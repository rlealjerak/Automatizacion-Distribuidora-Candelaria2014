variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "domain_name" {
  description = "Apex domain, registered externally (Namecheap) - this module only manages the hosted zone and records, not registration."
  type        = string
}

variable "subdomain" {
  description = "Subdomain label the backend API is served at, e.g. \"api\" for api.<domain_name>."
  type        = string
}

variable "alb_dns_name" {
  type = string
}

variable "alb_zone_id" {
  type = string
}
