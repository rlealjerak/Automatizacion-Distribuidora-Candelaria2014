############################################################
# S3 - supplier file storage
#
# Holds original uploaded supplier lists (immutable, versioned -
# never modified in place, per spec) under a `sources/` prefix,
# and generated exports under an `exports/` prefix. One bucket,
# separated by prefix, to keep infra simple for MVP; split into
# two buckets later if lifecycle/access policies need to diverge.
############################################################

resource "aws_s3_bucket" "supplier_files" {
  bucket = "${var.project_name}-${var.environment}-supplier-files"

  tags = {
    Name = "${var.project_name}-${var.environment}-supplier-files"
  }
}

resource "aws_s3_bucket_versioning" "supplier_files" {
  bucket = aws_s3_bucket.supplier_files.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "supplier_files" {
  bucket = aws_s3_bucket.supplier_files.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "supplier_files" {
  bucket                  = aws_s3_bucket.supplier_files.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Require TLS for all requests.
resource "aws_s3_bucket_policy" "supplier_files_tls_only" {
  bucket = aws_s3_bucket.supplier_files.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.supplier_files.arn,
          "${aws_s3_bucket.supplier_files.arn}/*",
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      }
    ]
  })
}
