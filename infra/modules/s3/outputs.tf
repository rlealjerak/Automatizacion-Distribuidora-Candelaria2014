output "bucket_name" {
  value = aws_s3_bucket.supplier_files.bucket
}

output "bucket_arn" {
  value = aws_s3_bucket.supplier_files.arn
}
