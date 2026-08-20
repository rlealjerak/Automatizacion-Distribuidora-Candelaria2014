module "network" {
  source = "./modules/network"

  project_name       = var.project_name
  environment        = var.environment
  vpc_cidr           = var.vpc_cidr
  availability_zones = var.availability_zones
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

  project_name               = var.project_name
  environment                = var.environment
  visibility_timeout_seconds = var.sqs_visibility_timeout_seconds
  max_receive_count          = var.sqs_max_receive_count
}

module "rds" {
  source = "./modules/rds"

  project_name            = var.project_name
  environment             = var.environment
  private_subnet_ids      = module.network.private_subnet_ids
  rds_security_group_id   = module.network.rds_security_group_id
  db_instance_class       = var.db_instance_class
  db_allocated_storage    = var.db_allocated_storage
  db_name                 = var.db_name
  db_username             = var.db_username
  deletion_protection     = var.db_deletion_protection
  skip_final_snapshot     = var.db_skip_final_snapshot
  backup_retention_period = var.db_backup_retention_period
}

module "ecr" {
  source = "./modules/ecr"

  project_name = var.project_name
  environment  = var.environment
}

module "alb" {
  source = "./modules/alb"

  project_name      = var.project_name
  environment       = var.environment
  vpc_id            = module.network.vpc_id
  public_subnet_ids = module.network.public_subnet_ids
  container_port    = var.container_port
}

# The one inbound rule into the ECS tasks security group - from the ALB
# only, on the container port. Defined here (not inside a module) since
# it's the seam between two independently-owned security groups
# (network's ecs_tasks SG, alb's own SG). See network/main.tf's docstring,
# which anticipated exactly this addition ("add one scoped to an ALB when
# the API needs to be reachable").
resource "aws_security_group_rule" "ecs_from_alb" {
  type                     = "ingress"
  from_port                = var.container_port
  to_port                  = var.container_port
  protocol                 = "tcp"
  security_group_id        = module.network.ecs_security_group_id
  source_security_group_id = module.alb.security_group_id
  description              = "Backend API from the ALB"
}

module "ecs_cluster" {
  source = "./modules/ecs_cluster"

  project_name  = var.project_name
  environment   = var.environment
  aws_region    = var.aws_region
  s3_bucket_arn = module.s3.bucket_arn
  sqs_queue_arn = module.sqs.queue_arn
  secret_arns = [
    module.secrets.sp_api_secret_arn,
    module.secrets.keepa_secret_arn,
    module.secrets.api_key_secret_arn,
    module.rds.master_user_secret_arn,
  ]

  container_image = "${module.ecr.repository_url}:${var.container_image_tag}"
  container_port  = var.container_port
  task_cpu        = var.ecs_task_cpu
  task_memory     = var.ecs_task_memory
  desired_count   = 1

  public_subnet_ids     = module.network.public_subnet_ids
  ecs_security_group_id = module.network.ecs_security_group_id
  target_group_arn      = module.alb.target_group_arn

  s3_bucket_name      = module.s3.bucket_name
  sqs_queue_url       = module.sqs.queue_url
  sp_api_secret_name  = module.secrets.sp_api_secret_name
  keepa_secret_name   = module.secrets.keepa_secret_name
  db_secret_name      = module.rds.master_user_secret_arn
  api_key_secret_name = module.secrets.api_key_secret_name
  sp_api_seller_id    = var.sp_api_seller_id
  db_host             = module.rds.address
  db_port             = module.rds.port
  db_name             = module.rds.db_name
  db_username         = var.db_username

  depends_on = [aws_security_group_rule.ecs_from_alb]
}
