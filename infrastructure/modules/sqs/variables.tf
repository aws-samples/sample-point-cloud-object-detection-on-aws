variable "queue_name" {
  type = string
}

variable "visibility_timeout" {
  type    = number
  default = 900
}

variable "max_receive_count" {
  type    = number
  default = 3
}

variable "message_retention_seconds" {
  type    = number
  default = 604800
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN for SQS server-side encryption"
  type        = string
}
