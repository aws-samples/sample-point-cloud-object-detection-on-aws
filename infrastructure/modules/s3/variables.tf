variable "prefix" { type = string }
variable "environment" { type = string }

variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN for SSE-KMS bucket encryption"
  type        = string
}

variable "cors_allowed_origins" {
  description = "Explicit allowlist of origins permitted to call CORS-enabled buckets (presigned upload flow). Empty (the default) configures no CORS rule at all. Never use '*'."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.cors_allowed_origins, "*")
    error_message = "cors_allowed_origins must not contain '*'. A wildcard lets any web origin drive the presigned-upload flow. List the deployed frontend origin(s) explicitly, e.g. [\"https://app.example.com\"]."
  }
}

variable "log_retention_days" {
  description = "Days to retain S3 server access logs"
  type        = number
  default     = 90
}

variable "manage_account_public_access_block" {
  description = "Manage the account-wide S3 Block Public Access setting (all four flags on). Off by default: it is an account setting, not part of this stack. Every bucket this module creates has its own public access block either way. Once on, Terraform refuses to delete it; see the comment in main.tf for how to stop managing it."
  type        = bool
  default     = false
}
