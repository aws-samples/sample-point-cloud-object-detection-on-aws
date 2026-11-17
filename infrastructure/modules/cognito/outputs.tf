output "user_pool_arn" { value = aws_cognito_user_pool.this.arn }
output "user_pool_id" { value = aws_cognito_user_pool.this.id }
output "client_id" { value = aws_cognito_user_pool_client.alb.id }
output "domain_prefix" { value = aws_cognito_user_pool_domain.this.domain }
output "failed_sign_in_alarm_name" { value = aws_cloudwatch_metric_alarm.failed_sign_ins.alarm_name }
