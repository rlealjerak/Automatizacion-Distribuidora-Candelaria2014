############################################################
# SQS - list processing job queue
#
# One message = one list-run job (not one row). The worker reads
# the run's raw line items from Postgres and processes them with
# per-row error isolation internally; SQS just needs to guarantee
# the run gets picked up and retried if the worker crashes.
############################################################

resource "aws_sqs_queue" "list_processing_dlq" {
  name                      = "${var.project_name}-${var.environment}-list-processing-dlq"
  message_retention_seconds = 1209600 # 14 days - long enough to notice and investigate failed runs
}

resource "aws_sqs_queue" "list_processing" {
  name                       = "${var.project_name}-${var.environment}-list-processing"
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = 345600 # 4 days

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.list_processing_dlq.arn
    maxReceiveCount      = var.max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "list_processing_dlq" {
  queue_url = aws_sqs_queue.list_processing_dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns    = [aws_sqs_queue.list_processing.arn]
  })
}
