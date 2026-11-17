output "cluster_name" {
  description = "EKS cluster name"
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS cluster API endpoint"
  value       = module.eks.cluster_endpoint
}

output "cluster_region" {
  description = "AWS region"
  value       = var.aws_region
}

output "ecr_repository_urls" {
  description = "ECR repository URLs"
  value       = module.ecr.repository_urls
}

output "s3_input_bucket" {
  description = "S3 bucket for point cloud uploads"
  value       = module.s3.input_bucket_name
}

output "s3_output_bucket" {
  description = "S3 bucket for detection results"
  value       = module.s3.output_bucket_name
}

output "s3_tiles_bucket" {
  description = "S3 bucket for LOD viewer tiles"
  value       = module.s3.tiles_bucket_name
}

output "s3_models_bucket" {
  description = "S3 bucket for model checkpoints"
  value       = module.s3.models_bucket_name
}

output "sqs_queue_url" {
  description = "SQS queue URL for detection jobs"
  value       = module.sqs.queue_url
}

output "jobs_table_name" {
  description = "DynamoDB table name for the shared job store"
  value       = module.dynamodb.table_name
}

output "vpc_id" {
  description = "VPC ID. deploy.sh passes it to the Load Balancer Controller, which cannot read it from instance metadata (IMDS hop limit 1)"
  value       = module.vpc.vpc_id
}

output "vpc_cidr" {
  description = "VPC CIDR. Worker egress NetworkPolicy excludes it (helm networkPolicy.vpcCidr)"
  value       = module.vpc.vpc_cidr
}

output "public_subnet_cidrs" {
  description = "ALB source CIDRs for the api NetworkPolicy (helm networkPolicy.albSourceCidrs)"
  value       = module.vpc.public_subnet_cidrs
}

output "iam_role_arns" {
  description = "IRSA role ARNs for service accounts"
  value       = module.iam.role_arns
}

output "configure_kubectl" {
  description = "Command to configure kubectl"
  value       = "aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${var.aws_region}"
}

output "bastion_instance_id" {
  description = "SSM relay instance ID (null when enable_bastion=false). It is a port-forwarding hop to the EKS API; see bastion_relay_commands."
  value       = try(module.bastion[0].instance_id, null)
}

output "bastion_relay_commands" {
  description = "Commands that reach the private EKS API through the SSM relay with your own IAM identity (null when enable_bastion=false): terraform output -raw bastion_relay_commands"
  value       = try(module.bastion[0].relay_commands, null)
}

output "bastion_operator_policy_json" {
  description = "IAM policy to attach to the operators who use the SSM relay (null when enable_bastion=false): a port-forwarding session to the EKS endpoint, not a shell on the relay"
  value       = try(module.bastion[0].operator_policy_json, null)
}

output "cluster_admin_principal_arns" {
  description = "IAM principals with cluster-admin access entries (cluster_admin_principal_arns, or by default the identity that first applied the stack)"
  value       = module.eks.cluster_admin_principal_arns
}

output "addon_role_arns" {
  description = "IRSA role ARNs for cluster addons (annotate the addon service accounts with these)"
  value = {
    alb_controller     = module.addons.alb_controller_role_arn
    cluster_autoscaler = module.addons.cluster_autoscaler_role_arn
    fluent_bit         = module.addons.fluent_bit_role_arn
    keda_operator      = module.addons.keda_operator_role_arn
  }
}

output "keda_namespace" {
  description = "Namespace the KEDA IRSA trust policy is scoped to. deploy.sh installs KEDA here."
  value       = module.addons.keda_namespace
}

# ---------------------------------------------------------------------------
# GPU capacity pools
# ---------------------------------------------------------------------------

output "gpu_pools" {
  description = "Per-pool summary of the GPU capacity ladder, including the AZs each pool's instance types are actually orderable in."
  value       = module.eks.gpu_pools
}

output "gpu_max_hourly_usd_estimate" {
  description = "Worst-case combined on-demand $/hr if every enabled GPU pool sat at max_size. Sanity check only, from operator-maintained per-node estimates."
  value       = module.eks.gpu_max_hourly_usd_estimate
}

output "gpu_cluster_autoscaler_priority_configmap" {
  description = "Cluster Autoscaler priority-expander ConfigMap. Apply before the cluster-autoscaler Helm release: terraform output -raw gpu_cluster_autoscaler_priority_configmap | kubectl apply -f -"
  value       = module.eks.gpu_cluster_autoscaler_priority_configmap
}

# ---------------------------------------------------------------------------
# Access mode
# ---------------------------------------------------------------------------

output "access_mode" {
  description = "public or port-forward. deploy.sh renders the frontend chart to match."
  value       = var.access_mode
}

output "app_host" {
  description = "Hostname of the public UI (lowercased INGRESS_HOST); empty in port-forward mode."
  value       = var.access_mode == "public" ? lower(var.app_host) : ""
}

output "port_forward_local_port" {
  description = "Local port for `kubectl port-forward svc/frontend <port>:80` in port-forward mode. The upload buckets accept browser uploads only from http://localhost:<port>, unless s3_cors_allowed_origins is set."
  value       = var.port_forward_local_port
}

output "app_client_group" {
  description = "Kubernetes group of app_client_principal_arns; the frontend chart's port-forward RoleBinding subject (helm portForward.group)."
  value       = local.app_client_group
}

output "cognito" {
  description = "ALB sign-in settings for the frontend chart (helm ingress.auth.*), and the pool ID for admin-create-user. Null in port-forward mode."
  value = var.access_mode == "public" ? {
    user_pool_arn           = module.cognito[0].user_pool_arn
    user_pool_id            = module.cognito[0].user_pool_id
    client_id               = module.cognito[0].client_id
    domain_prefix           = module.cognito[0].domain_prefix
    region                  = var.aws_region
    session_timeout_seconds = var.auth_session_hours * 3600
  } : null
}
