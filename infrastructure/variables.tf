variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
  default     = "pointcloud-detection"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "List of availability zones"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]
}

variable "cluster_version" {
  description = "Kubernetes version for EKS cluster (must be an EKS-supported version)"
  type        = string
  default     = "1.33"
}

variable "cpu_instance_types" {
  description = "EC2 instance types for CPU node group"
  type        = list(string)
  default     = ["m5.xlarge", "m5.2xlarge"]
}

# ---------------------------------------------------------------------------
# GPU capacity pools
# ---------------------------------------------------------------------------
# Replaces gpu_instance_types + gpu_min_size + gpu_max_size + the hardcoded
# capacity_type = "SPOT". See modules/eks/variables.tf for the full contract and
# the homogeneity requirement.
#
# Ordering note: priority is cheapest-first, EXCEPT that the legacy spot pool is
# deliberately ranked lowest. g5 spot capacity has proven unreliable here, so
# on-demand wins by default and spot is only a last-resort net. It costs nothing
# while min_size = 0.

# The type is spelled out here rather than using `any`: the pool entries have
# differing attribute sets (only some set enabled/require_ack/node_group_name),
# and `any` forces Terraform to unify all map elements into one object type,
# which fails on a heterogeneous map. The optional() attributes handle it.
# Validation rules live in modules/eks/variables.tf, at the single enforcement point.
variable "gpu_node_pools" {
  description = "Map of GPU node pools; one EKS managed node group per entry."
  type = map(object({
    instance_types     = list(string)
    gpu_count_per_node = number
    priority           = number

    capacity_type      = optional(string, "ON_DEMAND")
    accelerator_vendor = optional(string, "nvidia")
    cpu_arch           = optional(string, "x86_64")
    ami_type           = optional(string, "AL2023_x86_64_NVIDIA")
    min_size           = optional(number, 0)
    max_size           = optional(number, 2)
    root_volume_size   = optional(number, 200)
    enabled            = optional(bool, true)
    extra_labels       = optional(map(string), {})

    est_hourly_usd_per_node     = optional(number, 0)
    require_ack                 = optional(bool, false)
    node_group_name             = optional(string)
    launch_template_name_prefix = optional(string)
  }))

  default = {
    # Preserves the pre-refactor node group's Terraform state via
    # node_group_name + the moved block in modules/eks/gpu-nodegroup.tf.
    g5-spot = {
      instance_types          = ["g5.xlarge", "g5.2xlarge", "g4dn.xlarge"]
      gpu_count_per_node      = 1
      priority                = 5
      capacity_type           = "SPOT"
      max_size                = 3
      est_hourly_usd_per_node = 0.30
      node_group_name         = "pointcloud-detection-dev-gpu-inference"
    }
  }
}

variable "acknowledged_expensive_gpu_pools" {
  description = "Allowlist of gpu_node_pools keys carrying require_ack = true. An enabled require_ack pool not listed here fails at plan time."
  type        = list(string)
  default     = []
}

variable "cpu_min_size" {
  description = "Minimum number of CPU nodes"
  type        = number
  default     = 1
}

variable "cpu_max_size" {
  description = "Maximum number of CPU nodes"
  type        = number
  default     = 5
}

variable "cpu_desired_size" {
  description = "Desired number of CPU nodes"
  type        = number
  default     = 2
}



variable "enable_bastion" {
  description = "Deploy an SSM relay for operator access to the EKS endpoint, which is private only: kubectl and helm run on the operator's machine through a Session Manager port-forward (bastion_relay_commands output). Adds three Systems Manager interface endpoints in one Availability Zone. Without it, the API server is reachable only from inside the VPC."
  type        = bool
  default     = true
}

variable "cluster_admin_principal_arns" {
  description = "IAM role or user ARNs given cluster-admin through an EKS access entry each. Empty (the default) means the IAM principal that first applied the stack, which state records and keeps, so a later apply by another identity does not replace it. There is no implicit cluster-creator admin. Existing clusters: see modules/eks/variables.tf for adopting the creator's entry."
  type        = list(string)
  default     = []
}

variable "s3_cors_allowed_origins" {
  description = "Allowlist of web origins for CORS-enabled S3 buckets (presigned uploads). Empty (the default) derives it from the access mode: https://<app_host> for public, http://localhost:<port_forward_local_port> for port-forward. Never use '*'."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.s3_cors_allowed_origins, "*")
    error_message = "s3_cors_allowed_origins must not contain '*'. A wildcard lets any web origin drive the presigned-upload flow. List the deployed frontend origin(s) explicitly, e.g. [\"https://app.example.com\"]."
  }
}

variable "alarm_email" {
  description = "Email address for CloudWatch alarm notifications. Setting it also creates the alarm topic's KMS key."
  type        = string
  default     = ""
}

variable "manage_account_public_access_block" {
  description = "Also set the account-wide S3 Block Public Access (all four flags). Off by default, because it is an account setting shared with everything else in the account; check it with `aws s3control get-public-access-block --account-id <id>`. Every bucket in this stack has its own block regardless. Once on, destroy.sh leaves the setting in place."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# How users reach the application
# ---------------------------------------------------------------------------

variable "access_mode" {
  description = <<-EOT
    public: an internet-facing ALB on https://<app_host> with an ACM certificate, and
    the ALB's Cognito sign-in on every request. Creates the Cognito user pool.

    port-forward: no load balancer at all. Users run `kubectl port-forward` to the
    frontend service with their own IAM identity (see app_client_principal_arns).
    No domain or certificate needed.
  EOT
  type        = string
  default     = "public"

  validation {
    condition     = contains(["public", "port-forward"], var.access_mode)
    error_message = "access_mode must be \"public\" or \"port-forward\"."
  }
}

variable "app_host" {
  description = "Hostname users open in public mode (INGRESS_HOST), covered by the ACM certificate. Bare hostname: no scheme, port or path. Unused in port-forward mode."
  type        = string
  default     = ""

  validation {
    condition     = var.access_mode != "public" || can(regex("^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$", var.app_host))
    error_message = "access_mode is public, so app_host must be the bare hostname on the ACM certificate (e.g. pcd.example.com). deploy.sh passes INGRESS_HOST here."
  }
}

variable "auth_session_hours" {
  description = "How long an ALB sign-in lasts before the user signs in again. Long enough for a multi-hour upload; the Cognito refresh token is set to match. It is also how long a copied session cookie keeps working after the UI's Sign out, which revokes nothing, so shorten it where that matters."
  type        = number
  default     = 12
}

variable "cognito_failed_sign_in_alarm_threshold" {
  description = "Public mode: failed sign-ins within 5 minutes that raise the Cognito failed sign-in alarm, which notifies alarm_email when set."
  type        = number
  default     = 10
}

variable "cognito_mfa_configuration" {
  description = "ON (the default) or OPTIONAL (TOTP). ON requires every user to register an authenticator at first sign-in. OPTIONAL amounts to password-only sign-in, because the hosted page never prompts users to register one. See modules/cognito/variables.tf."
  type        = string
  default     = "ON"
}

variable "port_forward_local_port" {
  description = "Local port the documented `kubectl port-forward svc/frontend <port>:80` uses in port-forward mode. The S3 CORS origin is http://localhost:<port>, so the browser must open exactly that."
  type        = number
  default     = 8080
}

variable "app_client_principal_arns" {
  description = "IAM role or user ARNs, besides the cluster admins, allowed to port-forward to the UI and API. Each gets an EKS access entry in a group bound only to a namespaced port-forward Role. Each also needs IAM eks:DescribeCluster on this cluster to run `aws eks update-kubeconfig`; this stack does not grant it."
  type        = list(string)
  default     = []
}
