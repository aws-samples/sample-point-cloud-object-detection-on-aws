variable "cluster_name" { type = string }
variable "cluster_version" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "public_subnet_ids" { type = list(string) }
variable "cpu_instance_types" { type = list(string) }
variable "cpu_min_size" { type = number }
variable "cpu_max_size" { type = number }
variable "cpu_desired_size" { type = number }

# ---------------------------------------------------------------------------
# GPU capacity pools
# ---------------------------------------------------------------------------
# Replaces the former single (gpu_instance_types, gpu_min_size, gpu_max_size)
# triple + hardcoded capacity_type = "SPOT". One aws_eks_node_group per entry.
#
# Every pool carries workload-type=gpu, so existing Helm nodeSelectors keep
# working unchanged. Pools are additionally distinguished by a gpu-pool label
# for optional targeting.

variable "gpu_node_pools" {
  description = <<-EOT
    Map of GPU node pools; one EKS managed node group per entry.

    HOMOGENEITY REQUIREMENT: Cluster Autoscaler models a single node shape per
    node group. A pool mixing GPU counts (g5.xlarge=1 vs g5.12xlarge=4) or
    sizes (xlarge=4vCPU/16GiB vs 2xlarge=8vCPU/32GiB) makes that model wrong,
    and CA then mis-scales silently — it will happily provision a node the
    pending pod cannot fit on. Prefer one instance size per pool.

    priority: higher integer wins in the Cluster Autoscaler priority expander.
    Rank cheapest-first so the ladder walks up in cost, not down.
  EOT

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

    # Operator-maintained, for the plan-time cost output only. Not authoritative.
    est_hourly_usd_per_node = optional(number, 0)

    # Set true on pools expensive enough to require explicit opt-in; the pool
    # then only applies if its key is listed in var.acknowledged_expensive_gpu_pools.
    require_ack = optional(bool, false)

    # Pin the node group name to preserve existing Terraform state on refactor.
    # Leave unset for new pools.
    node_group_name = optional(string)

    # Pin the launch template name_prefix. name_prefix is ForceNew on
    # aws_launch_template, and launch_template.id is in turn ForceNew on
    # aws_eks_node_group — so letting the prefix change would replace the node
    # group. Set this to the pre-refactor prefix on a migrated pool.
    launch_template_name_prefix = optional(string)
  }))

  validation {
    condition = alltrue([
      for k, v in var.gpu_node_pools : contains(["SPOT", "ON_DEMAND"], v.capacity_type)
    ])
    error_message = "gpu_node_pools: capacity_type must be exactly \"SPOT\" or \"ON_DEMAND\"."
  }

  validation {
    condition = alltrue([
      for k, v in var.gpu_node_pools :
      v.accelerator_vendor != "nvidia" || !anytrue([
        for t in v.instance_types : startswith(t, "g4ad") || startswith(t, "g5g")
      ])
    ])
    error_message = <<-EOT
      gpu_node_pools: an nvidia pool must not contain g4ad or g5g instance types.
      g4ad is AMD Radeon Pro (ROCm, not CUDA) — nodes join Ready, the NVIDIA
      device plugin advertises nothing, and nvidia.com/gpu capacity stays 0
      forever. g5g is arm64 Graviton and needs AL2023_ARM_64_NVIDIA plus arm64
      container images.
    EOT
  }

  validation {
    condition = alltrue([
      for k, v in var.gpu_node_pools :
      v.accelerator_vendor != "nvidia" || !anytrue([
        for t in v.instance_types : can(regex("^(p2|p3|p3dn|g3|g3s)\\.", t))
      ])
    ])
    error_message = <<-EOT
      gpu_node_pools: an nvidia pool must not contain p2, p3, p3dn, g3 or g3s
      instance types. Their GPUs (K80, V100, M60) predate Turing, and the GPU
      image's torch CUDA 12.9 build and compiled extensions start at Turing
      (sm_75): the pods would start and then fail with "no kernel image is
      available". g4dn, g5, g6 and p4d are all supported.
    EOT
  }

  validation {
    condition = alltrue([
      for k, v in var.gpu_node_pools :
      v.cpu_arch != "arm64" || !can(regex("x86_64", v.ami_type))
    ])
    error_message = "gpu_node_pools: cpu_arch=\"arm64\" requires an ARM ami_type (e.g. AL2023_ARM_64_NVIDIA)."
  }

  validation {
    condition     = alltrue([for k, v in var.gpu_node_pools : v.gpu_count_per_node >= 1])
    error_message = "gpu_node_pools: gpu_count_per_node must be >= 1."
  }

  validation {
    condition     = alltrue([for k, v in var.gpu_node_pools : v.min_size <= v.max_size])
    error_message = "gpu_node_pools: min_size must be <= max_size."
  }

  validation {
    condition     = length(distinct([for k, v in var.gpu_node_pools : v.priority])) == length(var.gpu_node_pools)
    error_message = "gpu_node_pools: priority must be unique per pool, otherwise the Cluster Autoscaler priority expander ordering is ambiguous."
  }
}

variable "acknowledged_expensive_gpu_pools" {
  description = <<-EOT
    Allowlist of gpu_node_pools keys that carry require_ack = true. A pool with
    require_ack that is enabled but NOT listed here fails at plan time. This is
    the guard against a very expensive family (p4d.24xlarge is 8 GPUs at
    ~$21.96/hr on-demand in us-east-1) being enabled by accident.
  EOT
  type        = list(string)
  default     = []
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN for EKS secrets envelope encryption"
  type        = string
}

variable "app_client_principal_arns" {
  description = "IAM role or user ARNs allowed to port-forward to the application (UI and API). Each gets an access entry in app_client_group and nothing else. That authorizes Kubernetes API calls only: to build a kubeconfig with `aws eks update-kubeconfig`, each principal also needs IAM eks:DescribeCluster on this cluster, which this stack does not grant. Do not list a cluster admin (cluster_admin_principal_arns, or the Terraform caller by default): it already has an access entry, and a second entry for the same principal fails."
  type        = list(string)
  default     = []
}

variable "app_client_group" {
  description = "Kubernetes group the app_client_principal_arns map to. The frontend chart binds its port-forward Role to this group."
  type        = string
}

variable "cluster_admin_principal_arns" {
  description = <<-EOT
    IAM role or user ARNs given cluster-admin (AmazonEKSClusterAdminPolicy, cluster
    scope) through an access entry each. Empty (the default) means the IAM principal
    that first applied the stack, with an assumed-role session resolved to its role
    ARN. It is recorded in state, so a later apply by another identity keeps it rather
    than replacing it; that identity gets no access. List every admin here when more
    than one person or pipeline needs it.

    Clusters created before this variable existed already have an access entry for
    their creator, made by EKS at creation. Adopt it into state before applying, or
    the create fails with ResourceInUseException:
      terraform import 'module.eks.aws_eks_access_entry.cluster_admin["<arn>"]' '<cluster>:<arn>'
      terraform import 'module.eks.aws_eks_access_policy_association.cluster_admin["<arn>"]' '<cluster>#<arn>#arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy'
    `aws eks list-access-entries --cluster-name <cluster>` shows the ARN EKS recorded.
  EOT
  type        = list(string)
  default     = []
}
