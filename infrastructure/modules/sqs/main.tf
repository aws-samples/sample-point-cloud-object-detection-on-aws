# @secure_recommendation: encrypt both queues at rest with a customer-managed KMS key
# (SQS encryption baseline: a dedicated CMK rather than SQS-managed SSE). Setting kms_master_key_id
# implicitly disables SQS-managed SSE.
resource "aws_sqs_queue" "dlq" {
  name                              = "${var.queue_name}-dlq"
  message_retention_seconds         = 1209600 # 14 days
  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300

  tags = {
    Name = "${var.queue_name}-dlq"
  }
}

resource "aws_sqs_queue" "main" {
  name                              = var.queue_name
  visibility_timeout_seconds        = var.visibility_timeout
  message_retention_seconds         = var.message_retention_seconds
  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Name = var.queue_name
  }
}

# @secure_recommendation: deny any non-TLS (HTTP) access to the queues
# (enforce aws:SecureTransport for data in transit).
data "aws_iam_policy_document" "tls_only_main" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.main.arn]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

data "aws_iam_policy_document" "tls_only_dlq" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.dlq.arn]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "main" {
  queue_url = aws_sqs_queue.main.id
  policy    = data.aws_iam_policy_document.tls_only_main.json
}

resource "aws_sqs_queue_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  policy    = data.aws_iam_policy_document.tls_only_dlq.json
}
