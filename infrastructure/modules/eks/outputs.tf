output "cluster_name" {
  value = aws_eks_cluster.main.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.main.endpoint
}

output "cluster_arn" {
  value = aws_eks_cluster.main.arn
}

output "cluster_admin_principal_arns" {
  description = "IAM principals with a cluster-scoped AmazonEKSClusterAdminPolicy access entry"
  value       = sort(tolist(local.cluster_admin_principal_arns))
}

output "cluster_ca" {
  value = aws_eks_cluster.main.certificate_authority[0].data
}

output "oidc_provider_arn" {
  value = aws_iam_openid_connect_provider.eks.arn
}

output "oidc_issuer" {
  value = replace(aws_eks_cluster.main.identity[0].oidc[0].issuer, "https://", "")
}

output "node_role_arn" {
  value = aws_iam_role.node.arn
}

output "cluster_security_group_id" {
  description = "EKS-managed cluster security group ID (controls access to the API endpoint ENIs)"
  value       = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
}

# ---------------------------------------------------------------------------
# GPU capacity pool visibility
# ---------------------------------------------------------------------------

output "gpu_pools" {
  description = "Per-pool summary of the GPU capacity ladder that was actually created."
  value = {
    for k, v in local.gpu_pools_active : k => {
      node_group_name    = local.gpu_pool_node_group_name[k]
      instance_types     = v.instance_types
      capacity_type      = v.capacity_type
      gpu_count_per_node = v.gpu_count_per_node
      priority           = v.priority
      min_size           = v.min_size
      max_size           = v.max_size
      # AZs where these instance types are genuinely orderable. Compare against
      # the cluster's private subnet AZs: an AZ listed here that you have no
      # subnet in is unusable capacity, and a subnet AZ missing from this list
      # is an AZ where every scaling attempt will fail.
      # distinct(): the offerings API returns one row per (instance_type, AZ)
      # pair, so a pool with 3 instance types repeats each AZ up to 3 times.
      offered_in_azs = distinct(sort(data.aws_ec2_instance_type_offerings.gpu[k].locations))
    }
  }
}

output "gpu_cluster_autoscaler_priority_configmap" {
  description = <<-EOT
    Ready-to-apply Cluster Autoscaler priority-expander ConfigMap. Apply this
    BEFORE (or with) the cluster-autoscaler Helm release: if the ConfigMap is
    absent when CA starts, the priority expander degrades to effectively
    unranked selection and you silently get the old random behaviour while
    believing you have a cost-ordered ladder.
  EOT
  value = yamlencode({
    apiVersion = "v1"
    kind       = "ConfigMap"
    metadata = {
      name      = "cluster-autoscaler-priority-expander"
      namespace = "kube-system"
    }
    data = {
      priorities = local.gpu_ca_priorities_yaml
    }
  })
}

output "gpu_max_hourly_usd_estimate" {
  description = "Worst-case combined on-demand $/hr if every enabled GPU pool sat at max_size. Derived from the operator-maintained est_hourly_usd_per_node field, so treat it as a sanity check, not a bill."
  value       = local.gpu_max_hourly_usd
}
