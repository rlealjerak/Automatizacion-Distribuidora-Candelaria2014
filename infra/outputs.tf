output "vpc_id" {
  value = module.network.vpc_id
}

output "s3_bucket_name" {
  value = module.s3.bucket_name
}

output "sqs_queue_url" {
  value = module.sqs.queue_url
}

output "sqs_dlq_url" {
  value = module.sqs.dlq_url
}

output "rds_endpoint" {
  value     = module.rds.endpoint
  sensitive = true
}

output "rds_master_secret_arn" {
  value = module.rds.master_user_secret_arn
}

output "sp_api_secret_name" {
  value = module.secrets.sp_api_secret_name
}

output "keepa_secret_name" {
  value = module.secrets.keepa_secret_name
}

output "ecr_repository_url" {
  value = module.ecr.repository_url
}

output "ecs_cluster_name" {
  value = module.ecs_cluster.cluster_name
}

output "alb_dns_name" {
  description = "HTTP-only for now - see modules/alb/main.tf on why. http://<this>/health for a quick check; every other route needs X-Api-Key."
  value       = module.alb.dns_name
}

output "api_key_secret_name" {
  value = module.secrets.api_key_secret_name
}
