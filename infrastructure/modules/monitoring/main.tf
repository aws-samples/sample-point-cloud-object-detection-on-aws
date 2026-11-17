# @secure_recommendation: encrypt the log group with a CMK (logging security baseline).
resource "aws_cloudwatch_log_group" "eks" {
  name              = "/eks/${var.cluster_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

# @secure_recommendation: encrypt the alarm topic with a CMK. Alarm messages carry
# queue names, alarm ARNs and the account ID. The key is a customer managed key whose
# policy admits cloudwatch.amazonaws.com (modules/kms). With alias/aws/sns the alarm
# actions would fail and no notification would arrive, because that key's policy
# cannot be changed to name the CloudWatch principal.
resource "aws_sns_topic" "alarms" {
  count             = var.alarm_email != "" ? 1 : 0
  name              = "${var.cluster_name}-alarms"
  kms_master_key_id = var.sns_kms_key_arn

  lifecycle {
    precondition {
      condition     = var.sns_kms_key_arn != null && var.sns_kms_key_arn != ""
      error_message = "alarm_email is set, so the alarm topic is created and needs sns_kms_key_arn (the root module passes module.kms.sns_key_arn, created when alarm_email is set)."
    }
  }
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alarm_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alarms[0].arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

resource "aws_cloudwatch_metric_alarm" "queue_depth_high" {
  alarm_name          = "${var.cluster_name}-queue-depth-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Average"
  threshold           = 10
  alarm_description   = "SQS queue depth exceeds 10 messages for 10 minutes"
  alarm_actions       = var.alarm_email != "" ? [aws_sns_topic.alarms[0].arn] : []

  dimensions = {
    QueueName = var.queue_name
  }
}

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${var.cluster_name}-dlq-not-empty"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Dead letter queue has messages — failed jobs require investigation"
  alarm_actions       = var.alarm_email != "" ? [aws_sns_topic.alarms[0].arn] : []

  dimensions = {
    QueueName = var.dlq_name
  }
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = var.cluster_name

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "SQS Queue Depth"
          metrics = [["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.queue_name]]
          period  = 60
          stat    = "Average"
          region  = data.aws_region.current.region
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "DLQ Messages"
          metrics = [["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.dlq_name]]
          period  = 60
          stat    = "Sum"
          region  = data.aws_region.current.region
        }
      }
    ]
  })
}

data "aws_region" "current" {}
