variable "prefix" {
  description = "Prefix for resource names; also the start of the sign-in domain prefix"
  type        = string
}

variable "region" {
  description = "AWS Region the pool lives in (makes the domain prefix unique per Region)"
  type        = string
}

variable "app_host" {
  description = "Public hostname users open (INGRESS_HOST). Callback and sign-out URLs are built from it."
  type        = string
}

variable "session_hours" {
  description = "ALB session length in hours. The refresh token lasts this long so the ALB can keep the session alive for multi-hour uploads."
  type        = number
  default     = 12

  validation {
    condition     = var.session_hours >= 1 && var.session_hours <= 168
    error_message = "session_hours must be between 1 and 168 (the ALB's maximum session of 7 days)."
  }
}

variable "access_token_minutes" {
  description = "Access and ID token lifetime in minutes. Bounds how long a user keeps access after admin-disable-user or admin-user-global-sign-out. The UI's Sign out revokes no token and is not bounded by this."
  type        = number
  default     = 15

  validation {
    condition     = var.access_token_minutes >= 5 && var.access_token_minutes <= 1440
    error_message = "access_token_minutes must be between 5 and 1440, Cognito's allowed range."
  }
}

variable "mfa_configuration" {
  description = <<-EOT
    ON (the default) or OPTIONAL. TOTP is the only factor configured.

    ON makes the hosted page walk every user through TOTP registration at first
    sign-in. OPTIONAL lets users who have registered an authenticator use it, but the
    hosted page never prompts anyone to register one and this app has no page that
    does, so in practice OPTIONAL means password-only sign-in.
  EOT
  type        = string
  default     = "ON"

  validation {
    condition     = contains(["OPTIONAL", "ON"], var.mfa_configuration)
    error_message = "mfa_configuration must be OPTIONAL or ON."
  }
}

variable "alarm_topic_arn" {
  description = "SNS topic the failed sign-in alarm notifies. Empty (the default) creates the alarm with no action; the root module passes the monitoring topic, which exists when alarm_email is set."
  type        = string
  default     = ""
}

variable "failed_sign_in_threshold" {
  description = "Failed sign-ins to the ALB app client within 5 minutes that raise the alarm. Every failure counts, including a mistyped password or TOTP code, so this sits above what a handful of people mistyping produces."
  type        = number
  default     = 10

  validation {
    condition     = var.failed_sign_in_threshold >= 1
    error_message = "failed_sign_in_threshold must be at least 1."
  }
}
