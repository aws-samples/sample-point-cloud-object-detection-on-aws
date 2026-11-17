# ---------------------------------------------------------------------------
# GPU capacity pools
# ---------------------------------------------------------------------------
# One aws_eks_node_group per entry in var.gpu_node_pools.
#
# WHY for_each INSTEAD OF ONE NODE GROUP:
#   A single node group cannot honestly hold heterogeneous GPU families.
#   Cluster Autoscaler simulates scheduling against ONE modelled node shape per
#   node group; if the group can return either a 1-GPU g4dn.xlarge or an 8-GPU
#   p4d.24xlarge, the model is wrong for at least one of them and CA mis-scales
#   without surfacing an error. Splitting into homogeneous pools makes the
#   node-template math exact.
#
# WHY capacity_type IS NOW PER-POOL:
#   It used to be a hardcoded "SPOT". EKS managed node group configuration is
#   immutable (AWS documents nodegroup immutability), so flipping SPOT ->
#   ON_DEMAND in place forces a node group REPLACEMENT. Modelling capacity as
#   additive pools lets on-demand capacity be introduced with zero disruption to
#   the existing spot pool, which can then be disabled with `enabled = false`.
#
# SCHEDULING CONTRACT:
#   Every pool carries workload-type=gpu and the nvidia.com/gpu taint, identical
#   to the previous single group. Existing Helm nodeSelectors/tolerations work
#   unchanged. Pools are additionally labelled gpu-pool=<key> for the cases where
#   a workload genuinely needs to pin to one family.

locals {
  # Pools that will actually be created.
  gpu_pools_active = { for k, v in var.gpu_node_pools : k => v if v.enabled }

  # Node group name: honour an explicit override so a refactor can preserve
  # existing Terraform state, otherwise derive it from the pool key.
  gpu_pool_node_group_name = {
    for k, v in local.gpu_pools_active :
    k => coalesce(v.node_group_name, "${var.cluster_name}-gpu-${k}")
  }

  # Launch template name_prefix, same reasoning as the node group name: changing
  # it replaces the launch template, and a new launch template id replaces the
  # node group with it.
  gpu_pool_lt_name_prefix = {
    for k, v in local.gpu_pools_active :
    k => coalesce(v.launch_template_name_prefix, "${var.cluster_name}-gpu-${k}-")
  }

  # Kubernetes node labels per pool. workload-type=gpu is the load-bearing one:
  # it is what the backend worker and training Job select on today.
  gpu_pool_labels = {
    for k, v in local.gpu_pools_active : k => merge(
      {
        "workload-type"  = "gpu"
        "nvidia.com/gpu" = "true"
        "gpu-pool"       = k
      },
      v.extra_labels,
    )
  }

  # Cluster Autoscaler node-template ASG tags.
  #
  # THIS FIXES A REAL PRE-EXISTING BUG: these tags were absent entirely. With
  # min_size = 0 there is no live node for CA to introspect, so without the
  # node-template hints CA cannot simulate whether a pending GPU pod would fit,
  # tolerate the taint, or find nvidia.com/gpu capacity — and scale-from-zero
  # 0 -> 1 silently never triggers. The pod just sits Pending with no error.
  #
  # They are DERIVED from gpu_pool_labels and gpu_count_per_node rather than
  # hand-written, so the advertised template and the real node config are two
  # renderings of one source and cannot drift apart.
  gpu_pool_ca_tags = {
    for k, v in local.gpu_pools_active : k => merge(
      {
        for label_key, label_value in local.gpu_pool_labels[k] :
        "k8s.io/cluster-autoscaler/node-template/label/${label_key}" => label_value
      },
      {
        "k8s.io/cluster-autoscaler/node-template/taint/nvidia.com/gpu"     = "true:NoSchedule"
        "k8s.io/cluster-autoscaler/node-template/resources/nvidia.com/gpu" = tostring(v.gpu_count_per_node)
      },
    )
  }

  # Cluster Autoscaler priority-expander ConfigMap body.
  #
  # Higher integer wins. The trailing `1: - .*` catch-all is NOT cosmetic: a node
  # group matching no regex raises PriorityConfigMapNotMatchedGroup and is dropped
  # from consideration entirely, so without it a newly added or renamed group
  # silently becomes invisible to the autoscaler.
  #
  # Regexes match the underlying ASG name (eks-<nodegroup>-<uuid>), not the EKS
  # managed node group name.
  gpu_ca_priorities_yaml = join("\n", concat(
    flatten([
      for k in sort(keys(local.gpu_pools_active)) : [
        "${local.gpu_pools_active[k].priority}:",
        "  - ^eks-${local.gpu_pool_node_group_name[k]}-.*",
      ]
    ]),
    ["1:", "  - .*"],
  ))

  # Plan-time visibility only, from the operator-maintained est_hourly_usd_per_node.
  gpu_max_hourly_usd = sum(concat(
    [0],
    [for k, v in local.gpu_pools_active : v.est_hourly_usd_per_node * v.max_size],
  ))
}

# Which AZs actually offer each pool's instance types.
#
# This is the check that catches a family being ordered that simply does not
# exist in the region: p3.2xlarge and p3.8xlarge, for example, are offered in
# ZERO us-east-1 availability zones. Without this, `terraform apply` succeeds,
# the node group is created, and the failure only ever surfaces as a repeated
# failed scaling activity buried in the EC2 Auto Scaling console.
data "aws_ec2_instance_type_offerings" "gpu" {
  for_each = local.gpu_pools_active

  location_type = "availability-zone"

  filter {
    name   = "instance-type"
    values = each.value.instance_types
  }
}

resource "aws_eks_node_group" "gpu" {
  for_each = local.gpu_pools_active

  cluster_name    = aws_eks_cluster.main.name
  node_group_name = local.gpu_pool_node_group_name[each.key]
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids

  instance_types = each.value.instance_types
  capacity_type  = each.value.capacity_type
  # AL2 GPU AMIs are only supported on k8s <= 1.32. For 1.33+ use the AL2023 NVIDIA AMI type.
  ami_type = each.value.ami_type

  # @secure_recommendation: launch template enforces IMDSv2 + encrypted EBS.
  launch_template {
    id      = aws_launch_template.gpu[each.key].id
    version = aws_launch_template.gpu[each.key].latest_version
  }

  scaling_config {
    min_size     = each.value.min_size
    max_size     = each.value.max_size
    desired_size = each.value.min_size # Start at min (typically 0)
  }

  labels = local.gpu_pool_labels[each.key]

  taint {
    key    = "nvidia.com/gpu"
    value  = "true"
    effect = "NO_SCHEDULE"
  }

  tags = merge(
    {
      "k8s.io/cluster-autoscaler/enabled"             = "true"
      "k8s.io/cluster-autoscaler/${var.cluster_name}" = "owned"
    },
    local.gpu_pool_ca_tags[each.key],
  )

  lifecycle {
    # The cluster autoscaler owns the node count once the group exists. Without this,
    # an apply while GPU jobs ran set the count back to min_size, and EKS removed the
    # nodes running them.
    ignore_changes = [scaling_config[0].desired_size]

    # Block on conditions that are provably dead rather than creating a node
    # group that can never launch an instance.
    precondition {
      condition     = length(data.aws_ec2_instance_type_offerings.gpu[each.key].locations) > 0
      error_message = "GPU pool '${each.key}': none of ${join(", ", each.value.instance_types)} are offered in any availability zone in this region. This node group would be created successfully and then never launch an instance."
    }

    precondition {
      condition     = !each.value.require_ack || contains(var.acknowledged_expensive_gpu_pools, each.key)
      error_message = "GPU pool '${each.key}' is marked require_ack and is enabled, but is not listed in var.acknowledged_expensive_gpu_pools. Add \"${each.key}\" to that list to confirm you intend to pay for it."
    }

    precondition {
      condition     = length(each.value.instance_types) > 0
      error_message = "GPU pool '${each.key}': instance_types must not be empty."
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.node_worker,
    aws_iam_role_policy_attachment.node_cni,
    aws_iam_role_policy_attachment.node_ecr,
  ]
}

# Preserve state for the pre-refactor single GPU node group and its launch
# template, so converting to for_each does not destroy and recreate them.
moved {
  from = aws_eks_node_group.gpu
  to   = aws_eks_node_group.gpu["g5-spot"]
}

moved {
  from = aws_launch_template.gpu
  to   = aws_launch_template.gpu["g5-spot"]
}

# EKS Addons
resource "aws_eks_addon" "vpc_cni" {
  cluster_name = aws_eks_cluster.main.name
  addon_name   = "vpc-cni"

  resolve_conflicts_on_update = "OVERWRITE"

  # @secure_recommendation: without this the VPC CNI does not run its network policy
  # agent, so every NetworkPolicy in the Helm charts is accepted by the API server
  # and enforced by nothing -- pod-to-pod traffic stays fully open.
  configuration_values = jsonencode({
    enableNetworkPolicy = "true"
  })
}

resource "aws_eks_addon" "coredns" {
  cluster_name = aws_eks_cluster.main.name
  addon_name   = "coredns"

  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.cpu]
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name = aws_eks_cluster.main.name
  addon_name   = "kube-proxy"

  resolve_conflicts_on_update = "OVERWRITE"
}
