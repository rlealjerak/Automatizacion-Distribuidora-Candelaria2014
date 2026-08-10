module "network" {
  source = "./modules/network"

  project_name        = var.project_name
  environment         = var.environment
  vpc_cidr            = var.vpc_cidr
  availability_zones  = var.availability_zones
}

module "s3" {
  source = "./modules/s3"

  project_name = var.project_name
  environment  = var.environment
}

module "secrets" {
  source = "./modules/secrets"

  project_name = var.project_name
  environment  = var.environment
}

module "sqs" {
  source = "./modules/sqs"

  project_name                = var.project_name
  environment                 = var.environment
  visibility_timeout_seconds  = var.sqs_visibility_timeout_seconds
  max_receive_count           = var.sqs_max_receive_count
}

module "rds" {
  source = "./modules/rds"

  project_name           = var.project_name
  environment             = var.environment
  private_subnet_ids      = module.network.private_subnet_ids
  rds_security_group_id   = module.network.rds_security_group_id
  db_instance_class       = var.db_instance_class
  db_allocated_storage    = var.db_allocated_storage
  db_name                  = var.db_name
  db_username              = var.db_username
  deletion_protection      = var.db_deletion_protection
  skip_final_snapshot      = var.db_skip_final_snapshot
  backup_retention_period  = var.db_backup_retention_period
}

module "ecr" {
  source = "./modules/ecr"

  project_name = var.project_name
  environment  = var.environment
}

module "ecs_cluster" {
  source = "./modules/ecs_cluster"

  project_name  = var.project_name
  environment   = var.environment
  s3_bucket_arn = module.s3.bucket_arn
  sqs_queue_arn = module.sqs.queue_arn
  secret_arns = [
    module.secrets.sp_api_secret_arn,
    module.secrets.keepa_secret_arn,
    module.rds.master_user_secret_arn,
  ]
}
