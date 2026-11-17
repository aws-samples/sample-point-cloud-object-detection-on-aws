resource "aws_iam_role" "cluster" {
  name = "${var.cluster_name}-cluster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "eks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role_policy_attachment" "cluster_vpc_controller" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController"
  role       = aws_iam_role.cluster.name
}

# @secure_recommendation: the EKS cluster role needs use of the secrets CMK for
# envelope encryption. Granted least-privilege (no wildcard resource).
#
# No kms:CreateGrant: the cluster role uses the key, it does not delegate it. The grant
# EKS needs is created when the cluster is, with the credentials of whoever creates it,
# which is why the EKS guide warns against narrowing that caller's kms:CreateGrant with
# kms:GrantIsForAWSResource.
resource "aws_iam_role_policy" "cluster_kms" {
  name = "${var.cluster_name}-cluster-kms"
  role = aws_iam_role.cluster.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Encrypt", "kms:Decrypt", "kms:DescribeKey", "kms:ListGrants"]
      Resource = var.kms_key_arn
    }]
  })
}

resource "aws_eks_cluster" "main" {
  name     = var.cluster_name
  version  = var.cluster_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = concat(var.private_subnet_ids, var.public_subnet_ids)
    endpoint_private_access = true

    # @secure_recommendation: the API server has no public endpoint. Operators reach it
    # from their own machine through the SSM relay (the bastion module), under their own
    # IAM identity; nothing outside the VPC can reach it at all.
    endpoint_public_access = false
  }

  # @secure_recommendation: envelope-encrypt Kubernetes Secrets in etcd with a CMK.
  encryption_config {
    provider {
      key_arn = var.kms_key_arn
    }
    resources = ["secrets"]
  }

  # API access entries (least-privilege cluster access via IAM) while retaining the
  # aws-auth ConfigMap for compatibility.
  #
  # @secure_recommendation: no implicit cluster-creator admin. EKS would otherwise make
  # whoever ran the first apply a permanent cluster-admin through an access entry
  # nothing in this configuration declares. Admins are the explicit entries below
  # (var.cluster_admin_principal_arns), which can be read, reviewed and removed here.
  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = false
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  lifecycle {
    # bootstrap_cluster_creator_admin_permissions is only read by CreateCluster, and the
    # provider marks it ForceNew. Without this, changing it from true to false would
    # destroy and recreate every existing cluster. Existing clusters keep the creator's
    # entry EKS made at creation; see cluster_admin_principal_arns for adopting or
    # removing it.
    ignore_changes = [access_config[0].bootstrap_cluster_creator_admin_permissions]
  }

  depends_on = [
    aws_iam_role_policy_attachment.cluster_policy,
    aws_iam_role_policy_attachment.cluster_vpc_controller,
    aws_iam_role_policy.cluster_kms,
  ]
}

# OIDC Provider for IRSA
data "tls_certificate" "eks" {
  url = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

# Node IAM Role
resource "aws_iam_role" "node" {
  name = "${var.cluster_name}-node-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "node_worker" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
  role       = aws_iam_role.node.name
}

resource "aws_iam_role_policy_attachment" "node_cni" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
  role       = aws_iam_role.node.name
}

resource "aws_iam_role_policy_attachment" "node_ecr" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
  role       = aws_iam_role.node.name
}

# CPU Node Group
resource "aws_eks_node_group" "cpu" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${var.cluster_name}-cpu-workers"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids

  instance_types = var.cpu_instance_types
  capacity_type  = "ON_DEMAND"

  # @secure_recommendation: launch template enforces IMDSv2 + encrypted EBS.
  launch_template {
    id      = aws_launch_template.cpu.id
    version = aws_launch_template.cpu.latest_version
  }

  scaling_config {
    min_size     = var.cpu_min_size
    max_size     = var.cpu_max_size
    desired_size = var.cpu_desired_size
  }

  # The cluster autoscaler owns the node count once the group exists (the tags below);
  # desired_size only sets where it starts. Without this, every apply reset the count
  # the autoscaler had chosen, and deploy.sh stopped to ask (found on a cluster).
  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }

  labels = {
    "workload-type" = "cpu"
  }

  tags = {
    "k8s.io/cluster-autoscaler/enabled"             = "true"
    "k8s.io/cluster-autoscaler/${var.cluster_name}" = "owned"
  }

  depends_on = [
    aws_iam_role_policy_attachment.node_worker,
    aws_iam_role_policy_attachment.node_cni,
    aws_iam_role_policy_attachment.node_ecr,
  ]
}

# People and CI roles that reach the application through `kubectl port-forward`
# rather than the ALB: the only way in for port-forward mode, and the only
# programmatic path in either mode. An access entry with a Kubernetes group and no
# access policy grants nothing by itself. What the group may do is the namespaced
# Role in helm/frontend rbac-port-forward.yaml: read pods and services, open
# port-forwards. No exec, no Secrets, no Ingress writes. AmazonEKSEditPolicy is
# deliberately not used; it also grants pods/exec and Secrets, which would reach
# the worker's IRSA credentials.
resource "aws_eks_access_entry" "app_clients" {
  for_each = toset(var.app_client_principal_arns)

  cluster_name      = aws_eks_cluster.main.name
  principal_arn     = each.value
  type              = "STANDARD"
  kubernetes_groups = [var.app_client_group]
}

# ---------------------------------------------------------------------------
# Cluster administrators
# ---------------------------------------------------------------------------
# Replaces the implicit cluster-creator admin. Empty cluster_admin_principal_arns (the
# default) means the IAM principal that first applied this module, resolved from an
# assumed-role session to its role ARN, so a first apply leaves its operator able to
# run deploy.sh. That principal is recorded in state (terraform_data.first_admin) and
# kept: a later apply by another identity, a second operator or a CI role, does not
# replace it and lock the first one out. Set the variable explicitly where more than
# one person or pipeline administers the cluster.
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_iam_session_context" "current" {
  arn = data.aws_caller_identity.current.arn
}

# The first applier. ignore_changes keeps the input it was created with whoever
# applies later; replace it (terraform apply -replace) to hand the default to the
# current caller on purpose.
resource "terraform_data" "first_admin" {
  input = data.aws_iam_session_context.current.issuer_arn

  lifecycle {
    ignore_changes = [input]
  }
}

locals {
  cluster_admin_principal_arns = toset(
    length(var.cluster_admin_principal_arns) > 0
    ? var.cluster_admin_principal_arns
    : [terraform_data.first_admin.input]
  )
}

resource "aws_eks_access_entry" "cluster_admin" {
  for_each = local.cluster_admin_principal_arns

  cluster_name  = aws_eks_cluster.main.name
  principal_arn = each.value
  type          = "STANDARD"

  lifecycle {
    # EKS allows one access entry per principal, so the same ARN in both lists fails
    # at apply with ResourceInUseException. Catch it at plan time instead.
    precondition {
      condition     = !contains(var.app_client_principal_arns, each.value)
      error_message = "${each.value} is both a cluster admin and in app_client_principal_arns. EKS allows one access entry per principal; a cluster admin can already port-forward, so remove it from app_client_principal_arns."
    }
  }
}

resource "aws_eks_access_policy_association" "cluster_admin" {
  for_each = local.cluster_admin_principal_arns

  cluster_name  = aws_eks_cluster.main.name
  principal_arn = aws_eks_access_entry.cluster_admin[each.key].principal_arn
  policy_arn    = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}
