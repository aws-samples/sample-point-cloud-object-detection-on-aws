variable "cluster_name" { type = string }
variable "oidc_provider_arn" { type = string }
variable "oidc_issuer" { type = string }
variable "namespace" { type = string }
variable "input_bucket_arn" { type = string }
variable "output_bucket_arn" { type = string }
variable "tiles_bucket_arn" { type = string }
variable "models_bucket_arn" { type = string }
variable "queue_arn" { type = string }

variable "jobs_table_arn" {
  description = "DynamoDB job store table ARN (backend read/write, worker status updates)"
  type        = string
}

variable "s3_kms_key_arn" {
  description = "KMS key ARN used for S3 SSE-KMS (IRSA roles need data-key access)"
  type        = string
}

variable "sqs_kms_key_arn" {
  description = "KMS key ARN used for SQS encryption (producer/consumer roles need data-key access)"
  type        = string
}

variable "dynamodb_kms_key_arn" {
  description = "KMS key ARN used for DynamoDB encryption (job store read/write roles need data-key access)"
  type        = string
}
