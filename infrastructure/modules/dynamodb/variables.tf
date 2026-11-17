variable "table_name" {
  description = "Name of the DynamoDB table that stores job records"
  type        = string
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN for DynamoDB server-side encryption"
  type        = string
}

variable "point_in_time_recovery" {
  description = "Enable point-in-time recovery (continuous backups) for the table"
  type        = bool
  default     = true
}

variable "ttl_enabled" {
  description = "Enable DynamoDB TTL on the expires_at attribute so job records expire instead of accumulating (the job list is a full table Scan). The retention period itself is set application-side via JOBS_TTL_DAYS."
  type        = bool
  default     = true
}
