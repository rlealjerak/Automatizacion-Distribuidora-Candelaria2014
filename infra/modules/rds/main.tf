############################################################
# RDS PostgreSQL
#
# Master password is NOT set here. `manage_master_user_password`
# tells RDS to generate it and store/rotate it in Secrets Manager
# automatically - the password never appears in Terraform state,
# tfvars, or anywhere else. The app reads it from Secrets Manager
# at runtime via IAM, same as the SP-API/Keepa credentials.
############################################################

resource "aws_db_subnet_group" "main" {
  name       = "${var.project_name}-${var.environment}-db-subnets"
  subnet_ids = var.private_subnet_ids

  tags = {
    Name = "${var.project_name}-${var.environment}-db-subnets"
  }
}

resource "aws_db_instance" "main" {
  identifier     = "${var.project_name}-${var.environment}-db"
  engine         = "postgres"
  engine_version = "16"

  instance_class    = var.db_instance_class
  allocated_storage = var.db_allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username

  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [var.rds_security_group_id]
  publicly_accessible    = false
  multi_az               = false # MVP: single-AZ to control cost. Revisit if downtime risk becomes unacceptable.

  # 0 (no automated backups), not a real target - this AWS account rejected
  # any nonzero value with "FreeTierRestrictionError: The specified backup
  # retention period exceeds the maximum available to free tier customers",
  # confirmed via a real apply attempt (the account is evidently on some
  # kind of free-tier/restricted plan - AWS didn't report what the actual
  # allowed max is, just that >0 - possibly untested values - exceeded it).
  # Same category as deletion_protection/skip_final_snapshot below: fine
  # while iterating pre-launch, MUST be revisited (raise this and/or
  # upgrade the account's plan) before this holds real data anyone would
  # be upset to lose without a recovery point.
  backup_retention_period = var.backup_retention_period
  deletion_protection     = var.deletion_protection
  skip_final_snapshot     = var.skip_final_snapshot

  tags = {
    Name = "${var.project_name}-${var.environment}-db"
  }
}
