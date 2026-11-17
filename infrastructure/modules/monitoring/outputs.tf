output "log_group_name" { value = aws_cloudwatch_log_group.eks.name }
output "dashboard_name" { value = aws_cloudwatch_dashboard.main.dashboard_name }

output "sns_topic_arn" {
  description = "Alarm topic ARN; empty when alarm_email is unset. The Cognito sign-in alarm publishes here too."
  value       = var.alarm_email != "" ? aws_sns_topic.alarms[0].arn : ""
}
