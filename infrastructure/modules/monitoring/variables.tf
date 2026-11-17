variable "cluster_name" {
  type = string
}

variable "queue_name" {
  type = string
}

variable "dlq_name" {
  type = string
}

variable "alarm_email" {
  type    = string
  default = ""
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN for encrypting the CloudWatch log group"
  type        = string
}

variable "sns_kms_key_arn" {
  description = "Customer-managed KMS key ARN for the alarm SNS topic. Required when alarm_email is set; its key policy must let cloudwatch.amazonaws.com use it."
  type        = string
  default     = null
}
