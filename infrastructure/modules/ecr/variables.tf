variable "prefix" { type = string }

variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN for ECR repository encryption"
  type        = string
}
