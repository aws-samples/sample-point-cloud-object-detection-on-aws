locals {
  cluster_name = "${var.project_name}-${var.environment}"
  prefix       = "${var.project_name}-${var.environment}"

  # Origin the browser loads the UI from, which is also the only origin the upload
  # buckets accept presigned PUTs from. Lowercased because hostnames are, and the
  # Cognito callback built from the same value must be.
  app_origin = var.access_mode == "public" ? "https://${lower(var.app_host)}" : "http://localhost:${var.port_forward_local_port}"

  # Kubernetes group for app_client_principal_arns. The frontend chart binds its
  # port-forward Role to it; deploy.sh passes this output to Helm.
  app_client_group = "${var.project_name}-app-clients"
}

module "kms" {
  source = "./modules/kms"
  prefix = local.prefix

  # The alarm topic, and so its key, exists only when alarm_email is set.
  create_sns_key = var.alarm_email != ""
}

module "vpc" {
  source             = "./modules/vpc"
  project_name       = var.project_name
  environment        = var.environment
  vpc_cidr           = var.vpc_cidr
  availability_zones = var.availability_zones
  kms_key_arn        = module.kms.logs_key_arn
}

module "eks" {
  source             = "./modules/eks"
  cluster_name       = local.cluster_name
  cluster_version    = var.cluster_version
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  public_subnet_ids  = module.vpc.public_subnet_ids
  cpu_instance_types = var.cpu_instance_types
  cpu_min_size       = var.cpu_min_size
  cpu_max_size       = var.cpu_max_size
  cpu_desired_size   = var.cpu_desired_size

  gpu_node_pools                   = var.gpu_node_pools
  acknowledged_expensive_gpu_pools = var.acknowledged_expensive_gpu_pools
  kms_key_arn                      = module.kms.eks_key_arn
  app_client_principal_arns        = var.app_client_principal_arns
  app_client_group                 = local.app_client_group
  cluster_admin_principal_arns     = var.cluster_admin_principal_arns
}

module "s3" {
  source               = "./modules/s3"
  prefix               = local.prefix
  environment          = var.environment
  kms_key_arn          = module.kms.s3_key_arn
  cors_allowed_origins = length(var.s3_cors_allowed_origins) > 0 ? var.s3_cors_allowed_origins : [local.app_origin]

  manage_account_public_access_block = var.manage_account_public_access_block
}

module "sqs" {
  source      = "./modules/sqs"
  queue_name  = "${local.prefix}-jobs"
  kms_key_arn = module.kms.sqs_key_arn
}

module "ecr" {
  source      = "./modules/ecr"
  prefix      = var.project_name
  kms_key_arn = module.kms.ecr_key_arn
}

module "dynamodb" {
  source      = "./modules/dynamodb"
  table_name  = "${local.prefix}-jobs"
  kms_key_arn = module.kms.dynamodb_key_arn
}

module "iam" {
  source               = "./modules/iam"
  cluster_name         = local.cluster_name
  oidc_provider_arn    = module.eks.oidc_provider_arn
  oidc_issuer          = module.eks.oidc_issuer
  namespace            = "pointcloud-detection"
  input_bucket_arn     = module.s3.input_bucket_arn
  output_bucket_arn    = module.s3.output_bucket_arn
  tiles_bucket_arn     = module.s3.tiles_bucket_arn
  models_bucket_arn    = module.s3.models_bucket_arn
  queue_arn            = module.sqs.queue_arn
  jobs_table_arn       = module.dynamodb.table_arn
  s3_kms_key_arn       = module.kms.s3_key_arn
  sqs_kms_key_arn      = module.kms.sqs_key_arn
  dynamodb_kms_key_arn = module.kms.dynamodb_key_arn
}

module "monitoring" {
  source       = "./modules/monitoring"
  cluster_name = local.cluster_name
  queue_name   = module.sqs.queue_name
  dlq_name     = module.sqs.dlq_name
  alarm_email  = var.alarm_email
  kms_key_arn  = module.kms.logs_key_arn

  sns_kms_key_arn = module.kms.sns_key_arn
}

# @secure_recommendation: least-privilege IRSA roles for cluster addons (ALB controller,
# Cluster Autoscaler, Fluent Bit) instead of granting broad permissions to the node role.
module "addons" {
  source            = "./modules/addons"
  cluster_name      = local.cluster_name
  vpc_id            = module.vpc.vpc_id
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_issuer       = module.eks.oidc_issuer
  sqs_queue_arn     = module.sqs.queue_arn
}

# @secure_recommendation: SSM relay provides operator access to the EKS endpoint, which
# is private only. Operators port-forward through it to the API with their own IAM
# identity (the bastion_relay_commands output); the host itself holds no cluster
# access. Enabled by default; disable with enable_bastion=false only where operators
# already reach the VPC another way, such as a VPN.
module "bastion" {
  count  = var.enable_bastion ? 1 : 0
  source = "./modules/bastion"

  prefix                    = local.prefix
  vpc_id                    = module.vpc.vpc_id
  vpc_cidr                  = module.vpc.vpc_cidr
  private_subnet_id         = module.vpc.private_subnet_ids[0]
  cluster_name              = module.eks.cluster_name
  cluster_arn               = module.eks.cluster_arn
  cluster_endpoint          = module.eks.cluster_endpoint
  cluster_security_group_id = module.eks.cluster_security_group_id
}

# Sign-in for the internet-facing ALB. Only created in public mode: port-forward mode
# has no listener to put it on, and its gate is the caller's IAM identity instead.
module "cognito" {
  count  = var.access_mode == "public" ? 1 : 0
  source = "./modules/cognito"

  prefix            = local.prefix
  region            = var.aws_region
  app_host          = var.app_host
  session_hours     = var.auth_session_hours
  mfa_configuration = var.cognito_mfa_configuration

  # The failed sign-in alarm notifies the same topic as the other alarms, when there
  # is one (alarm_email set).
  alarm_topic_arn          = module.monitoring.sns_topic_arn
  failed_sign_in_threshold = var.cognito_failed_sign_in_alarm_threshold
}
