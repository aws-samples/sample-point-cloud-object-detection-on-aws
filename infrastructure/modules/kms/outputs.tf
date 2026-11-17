output "s3_key_arn" {
  description = "KMS key ARN for S3 bucket encryption"
  value       = aws_kms_key.s3.arn
  # Every output waits for the key policies, so nothing uses a key before its policy
  # admits the service it is for (CloudWatch Logs checks it when a log group is created
  # with the key).
  depends_on = [aws_kms_key_policy.this]
}

output "sqs_key_arn" {
  description = "KMS key ARN for SQS encryption"
  value       = aws_kms_key.sqs.arn
  depends_on  = [aws_kms_key_policy.this]
}

output "dynamodb_key_arn" {
  description = "KMS key ARN for DynamoDB job store encryption"
  value       = aws_kms_key.dynamodb.arn
  depends_on  = [aws_kms_key_policy.this]
}

output "eks_key_arn" {
  description = "KMS key ARN for EKS secrets envelope encryption"
  value       = aws_kms_key.eks.arn
  depends_on  = [aws_kms_key_policy.this]
}

output "logs_key_arn" {
  description = "KMS key ARN for CloudWatch Logs encryption"
  value       = aws_kms_key.logs.arn
  depends_on  = [aws_kms_key_policy.this]
}

output "ecr_key_arn" {
  description = "KMS key ARN for ECR repository encryption"
  value       = aws_kms_key.ecr.arn
  depends_on  = [aws_kms_key_policy.this]
}

output "sns_key_arn" {
  description = "KMS key ARN for the alarm SNS topic; null when create_sns_key is false"
  value       = try(aws_kms_key.sns[0].arn, null)
  depends_on  = [aws_kms_key_policy.sns]
}
