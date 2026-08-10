output "queue_url" {
  value = aws_sqs_queue.list_processing.id
}

output "queue_arn" {
  value = aws_sqs_queue.list_processing.arn
}

output "dlq_url" {
  value = aws_sqs_queue.list_processing_dlq.id
}

output "dlq_arn" {
  value = aws_sqs_queue.list_processing_dlq.arn
}
