aws_region         = "us-east-1"
project_name       = "pointcloud-detection"
environment        = "dev"
vpc_cidr           = "10.0.0.0/16"
availability_zones = ["us-east-1a", "us-east-1b", "us-east-1c"]
cluster_version    = "1.33"
cpu_instance_types = ["m5.xlarge", "m5.2xlarge"]
cpu_min_size       = 1
cpu_max_size       = 5
cpu_desired_size   = 2

# --- GPU capacity pools -----------------------------------------------------
# On-demand first. g5 spot proved unreliable in us-east-1, so the legacy spot
# pool is kept (state preserved, costs nothing at min_size 0) but ranked LOWEST
# so it is only ever a last resort.
#
# priority: higher integer = preferred by the Cluster Autoscaler priority
# expander. Ordered cheapest-first among on-demand.
#
# One instance SIZE per pool, deliberately. Cluster Autoscaler models one node
# shape per node group, so mixing xlarge (4 vCPU/16 GiB) with 2xlarge
# (8 vCPU/32 GiB) makes it scale up nodes the pending pod cannot fit on.
#
# est_hourly_usd_per_node: verified against the AWS Price List API for us-east-1
# Linux/shared on 2026-08-29. Feeds the gpu_max_hourly_usd_estimate output only.
#
# NOTE ON SIZING: the inference worker requests 2000m CPU / 8Gi and fits an
# xlarge. The training Job requests 4000m CPU / 16Gi, which does NOT fit any
# xlarge GPU node (4 vCPU / 16 GiB host, less kubelet reservation) — g6-2xl is
# the only pool below it can schedule on.
#
# p3 IS DELIBERATELY ABSENT: describe-instance-type-offerings returns zero
# availability zones for p3.2xlarge and p3.8xlarge in us-east-1. The family is
# not orderable here. The offerings precondition in the eks module will block
# the apply with a clear message if it is added back.
gpu_node_pools = {
  # ---- inference tier (xlarge: 4 vCPU / 16 GiB host) ----
  g4dn-xl = {
    instance_types          = ["g4dn.xlarge"]
    gpu_count_per_node      = 1
    priority                = 100
    max_size                = 2
    est_hourly_usd_per_node = 0.526
    extra_labels            = { gpu-model = "t4", gpu-vram-gb = "16" }
  }
  g6-xl = {
    instance_types          = ["g6.xlarge"]
    gpu_count_per_node      = 1
    priority                = 90
    max_size                = 2
    est_hourly_usd_per_node = 0.8048
    extra_labels            = { gpu-model = "l4", gpu-vram-gb = "24" }
  }
  g5-xl = {
    instance_types          = ["g5.xlarge"]
    gpu_count_per_node      = 1
    priority                = 80
    max_size                = 2
    est_hourly_usd_per_node = 1.006
    extra_labels            = { gpu-model = "a10g", gpu-vram-gb = "24" }
  }

  # ---- training tier (2xlarge: 8 vCPU / 32 GiB host) ----
  g6-2xl = {
    instance_types          = ["g6.2xlarge"]
    gpu_count_per_node      = 1
    priority                = 70
    max_size                = 1
    est_hourly_usd_per_node = 0.9776
    extra_labels            = { gpu-model = "l4", gpu-vram-gb = "24" }
  }

  # ---- multi-GPU, opt-in only ----
  # 8x A100 40GB at ~$21.96/hr. At ~2h/day that is the entire monthly budget in
  # under 3 hours. A pod requesting nvidia.com/gpu: 1 would idle 7 of 8 GPUs.
  # Enable by setting enabled = true AND adding "p4d" to
  # acknowledged_expensive_gpu_pools below.
  p4d = {
    instance_types          = ["p4d.24xlarge"]
    gpu_count_per_node      = 8
    priority                = 10
    max_size                = 1
    root_volume_size        = 500
    enabled                 = false
    require_ack             = true
    est_hourly_usd_per_node = 21.9576
    extra_labels            = { gpu-model = "a100", gpu-vram-gb = "40" }
  }

  # ---- legacy spot pool: state-preserved, ranked last ----
  # node_group_name pins the pre-refactor name so the moved block in
  # modules/eks/gpu-nodegroup.tf transfers state instead of destroying it.
  # Set enabled = false to remove it entirely (safe: min_size/desired_size are 0,
  # so there are no nodes to drain).
  g5-spot = {
    instance_types     = ["g5.xlarge", "g5.2xlarge", "g4dn.xlarge"]
    gpu_count_per_node = 1
    priority           = 5
    capacity_type      = "SPOT"
    max_size           = 3
    # Both overrides below pin pre-refactor names so this pool is migrated in
    # place rather than replaced. name_prefix is ForceNew on the launch template,
    # and launch_template.id is ForceNew on the node group.
    node_group_name             = "pointcloud-detection-dev-gpu-inference"
    launch_template_name_prefix = "pointcloud-detection-dev-gpu-"
    est_hourly_usd_per_node     = 0.30
  }
}

acknowledged_expensive_gpu_pools = []

# --- EKS endpoint access (dev) ---
# The cluster has a private API endpoint only. kubectl and helm run on your machine
# through the SSM relay, which this creates; `terraform output -raw
# bastion_relay_commands` prints how to start it (README, "Private cluster endpoint").
enable_bastion = true

# --- S3 CORS ---
# Empty derives the one origin the UI is served from: https://<app_host> in public mode,
# http://localhost:<port_forward_local_port> in port-forward mode. Set it only to add or
# replace origins. '*' is rejected by a validation block: it would let any web origin
# drive the presigned-upload flow.
s3_cors_allowed_origins = []

# --- Access mode ---
# access_mode and app_host come from deploy.sh (ACCESS_MODE, INGRESS_HOST), so they are
# not set here. List any extra IAM principals that should reach the UI and API through
# kubectl port-forward; the identity that creates the cluster already can.
app_client_principal_arns = []

alarm_email = "" # Set to receive CloudWatch alarm notifications
