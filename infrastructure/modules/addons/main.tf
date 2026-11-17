# @secure_recommendation: cluster addons that call the AWS API use dedicated IRSA roles with
# least-privilege policies scoped to the specific service account (via OIDC sub), NOT
# the shared node role. Each trust policy below names exactly one namespace and service
# account, and the Helm release in deploy.sh that installs the addon must create that
# account with the role's eks.amazonaws.com/role-arn annotation. The Cluster
# Autoscaler's is kube-system:cluster-autoscaler, which is not its chart's default
# name, so its release sets rbac.serviceAccount.name to match as well as annotating it
# with addon_role_arns.cluster_autoscaler.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  region    = data.aws_region.current.region
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
}

# Reusable IRSA trust policy generator, one entry per addon service account.
# Namespace is explicit per entry because KEDA does not live in kube-system.
data "aws_iam_policy_document" "assume" {
  for_each = {
    lbc  = { namespace = "kube-system", name = "aws-load-balancer-controller" }
    ca   = { namespace = "kube-system", name = "cluster-autoscaler" }
    fb   = { namespace = "kube-system", name = "aws-for-fluent-bit" }
    keda = { namespace = var.keda_namespace, name = var.keda_service_account }
  }

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.oidc_issuer}:sub"
      values   = ["system:serviceaccount:${each.value.namespace}:${each.value.name}"]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.oidc_issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------------------
# AWS Load Balancer Controller — official AWS IAM policy (v2.13.4), with two changes:
# the unconditioned security-group ingress statement is limited to security groups in
# this account, Region and cluster VPC (the controller only edits ingress on the load
# balancer and node security groups, which all live in the cluster VPC); and the
# unconditioned listener, rule, WAF association and Shield DeleteProtection actions are
# limited to this account and Region's load balancer, listener, rule, web ACL and
# protection ARNs. elasticloadbalancing:SetWebAcl and shield:CreateProtection support
# no resource-level permissions, so they stay on "*" as upstream.
# ---------------------------------------------------------------------------
resource "aws_iam_role" "lbc" {
  name               = "${var.cluster_name}-alb-controller"
  assume_role_policy = data.aws_iam_policy_document.assume["lbc"].json
}

# The official policy is 10,726 characters once rendered, over the IAM managed-policy
# size quota of 6,144 (which cannot be raised for a single policy). It is split across
# two managed policies, both attached to the one role, so the controller's effective
# permissions are unchanged. The split is by statement, along the natural read/write
# boundary: part 1 is the service-linked role, the Describe/Get reads, and the
# security-group statements; part 2 is the load balancer, target group, listener and
# rule create/modify/tag/register statements.
locals {
  lbc_policy_vars = {
    partition = local.partition
    region    = local.region
    account   = local.account
    vpc_arn   = "arn:${local.partition}:ec2:${local.region}:${local.account}:vpc/${var.vpc_id}"
  }
}

resource "aws_iam_policy" "lbc" {
  for_each    = toset(["1", "2"])
  name        = "${var.cluster_name}-alb-controller-${each.key}"
  description = "AWS Load Balancer Controller policy ${each.key}/2 (official v2.13.4, scoped to this account, Region and cluster VPC; split to fit the IAM policy size quota)"
  policy      = templatefile("${path.module}/alb-controller-iam-policy-${each.key}.json", local.lbc_policy_vars)
}

resource "aws_iam_role_policy_attachment" "lbc" {
  for_each   = aws_iam_policy.lbc
  role       = aws_iam_role.lbc.name
  policy_arn = each.value.arn
}

# ---------------------------------------------------------------------------
# Cluster Autoscaler — least-privilege: read-all describe + mutate only ASGs
# tagged for this cluster (matches node group autoscaler tags).
# ---------------------------------------------------------------------------
resource "aws_iam_role" "ca" {
  name               = "${var.cluster_name}-cluster-autoscaler"
  assume_role_policy = data.aws_iam_policy_document.assume["ca"].json
}

data "aws_iam_policy_document" "ca" {
  # Only actions that accept no resource narrower than "*" stay here: the Auto Scaling
  # and EC2 Describe* calls and GetInstanceTypesFromInstanceRequirements are list-style
  # reads that IAM authorizes on "*" alone. Anything that can be scoped is below.
  statement {
    sid    = "Describe"
    effect = "Allow"
    actions = [
      "autoscaling:DescribeAutoScalingGroups",
      "autoscaling:DescribeAutoScalingInstances",
      "autoscaling:DescribeLaunchConfigurations",
      "autoscaling:DescribeScalingActivities",
      "autoscaling:DescribeTags",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeLaunchTemplateVersions",
      "ec2:DescribeImages",
      "ec2:GetInstanceTypesFromInstanceRequirements",
    ]
    resources = ["*"]
  }

  # Managed node groups of this cluster only. Node group ARNs are
  # nodegroup/<cluster>/<nodegroup>/<id>.
  statement {
    sid       = "DescribeOwnNodegroups"
    effect    = "Allow"
    actions   = ["eks:DescribeNodegroup"]
    resources = ["arn:${local.partition}:eks:${local.region}:${local.account}:nodegroup/${var.cluster_name}/*/*"]
  }

  # Auto Scaling groups in this account and Region, and of those only the ones tagged
  # as owned by this cluster, which is every node group this stack creates.
  statement {
    sid    = "MutateOwnedASGs"
    effect = "Allow"
    actions = [
      "autoscaling:SetDesiredCapacity",
      "autoscaling:TerminateInstanceInAutoScalingGroup",
    ]
    resources = ["arn:${local.partition}:autoscaling:${local.region}:${local.account}:autoScalingGroup:*:autoScalingGroupName/*"]
    condition {
      test     = "StringEquals"
      variable = "autoscaling:ResourceTag/k8s.io/cluster-autoscaler/${var.cluster_name}"
      values   = ["owned"]
    }
  }
}

resource "aws_iam_role_policy" "ca" {
  name   = "cluster-autoscaler"
  role   = aws_iam_role.ca.id
  policy = data.aws_iam_policy_document.ca.json
}

# ---------------------------------------------------------------------------
# Fluent Bit — least-privilege CloudWatch Logs writes scoped to this cluster's
# log-group namespace.
# ---------------------------------------------------------------------------
resource "aws_iam_role" "fb" {
  name               = "${var.cluster_name}-fluent-bit"
  assume_role_policy = data.aws_iam_policy_document.assume["fb"].json
}

data "aws_iam_policy_document" "fb" {
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "logs:PutRetentionPolicy",
    ]
    resources = [
      "arn:${local.partition}:logs:${local.region}:${local.account}:log-group:/aws/eks/${var.cluster_name}/*",
      "arn:${local.partition}:logs:${local.region}:${local.account}:log-group:/aws/eks/${var.cluster_name}/*:*",
    ]
  }
}

resource "aws_iam_role_policy" "fb" {
  name   = "fluent-bit"
  role   = aws_iam_role.fb.id
  policy = data.aws_iam_policy_document.fb.json
}

# ---------------------------------------------------------------------------
# KEDA — turns SQS queue depth into worker replicas.
# ---------------------------------------------------------------------------
# WHY THIS EXISTS AT ALL:
#   Cluster Autoscaler scales NODES in response to Pending pods. Nothing in the
#   cluster was creating those pods from queue backlog. The worker HPA targeted an
#   external metric (sqs_queue_messages_visible) that no adapter published, so it
#   reported <unknown> and never scaled off minReplicas. EKS and SQS alone do not
#   close that loop; KEDA is the missing half.
#
# WHY KEDA RATHER THAN A CLOUDWATCH METRICS ADAPTER:
#   A plain HPA cannot reach 0 replicas without the HPAScaleToZero feature gate,
#   which EKS does not expose. With minReplicas pinned at 1 and the worker
#   requesting nvidia.com/gpu, one GPU node stays up permanently and the
#   scale-to-zero cost model does not hold. KEDA scales the Deployment 0 <-> N
#   itself, which is what actually lets the GPU pool drain back to zero.
#
# PERMISSIONS:
#   The aws-sqs-queue scaler only ever calls GetQueueAttributes. It does not read,
#   send, or delete messages — the worker's own IRSA role does that. No kms:Decrypt
#   is required because queue attributes are not encrypted payload.
resource "aws_iam_role" "keda" {
  name               = "${var.cluster_name}-keda-operator"
  assume_role_policy = data.aws_iam_policy_document.assume["keda"].json
}

data "aws_iam_policy_document" "keda" {
  statement {
    sid       = "ReadJobQueueDepth"
    effect    = "Allow"
    actions   = ["sqs:GetQueueAttributes"]
    resources = [var.sqs_queue_arn]
  }
}

resource "aws_iam_role_policy" "keda" {
  name   = "keda-sqs-scaler"
  role   = aws_iam_role.keda.id
  policy = data.aws_iam_policy_document.keda.json
}
