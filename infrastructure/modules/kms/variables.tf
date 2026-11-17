variable "prefix" {
  description = "Prefix for key names and aliases"
  type        = string
}

variable "deletion_window_in_days" {
  description = "KMS key deletion window (7-30 days)"
  type        = number
  default     = 30
}

variable "create_sns_key" {
  description = "Create the alarm SNS topic key. The root module sets this when alarm_email is set, which is when the topic exists."
  type        = bool
  default     = false
}
