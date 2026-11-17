# @secure_recommendation: Dedicated customer-managed KMS keys per data domain (S3, SQS,
# EKS secrets, logs) so encryption is under our control, auditable via CloudTrail, and
# access is governed by least-privilege IAM. Keys must be usable by the same principals
# that own the encrypted components (AWS security baseline practice).

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  partition  = data.aws_partition.current.partition
  root_arn   = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"

  # Every key but the alarm topic key, by name. Each gets its own key policy
  # (aws_kms_key_policy below), whose statements name that key's ARN as their resource.
  keys = merge(
    {
      s3       = aws_kms_key.s3
      sqs      = aws_kms_key.sqs
      dynamodb = aws_kms_key.dynamodb
      eks      = aws_kms_key.eks
      logs     = aws_kms_key.logs
      ecr      = aws_kms_key.ecr
    },
  )

  # The keys whose policy adds a service statement to the root one. The alarm topic key,
  # which exists only with its topic, has its own policy resource below.
  service_policies = {
    sqs  = data.aws_iam_policy_document.sqs.json
    logs = data.aws_iam_policy_document.logs.json
  }
}

# Standard "enable IAM policies" root statement, one per key. Delegates access control
# to IAM so the IRSA/cluster roles can be granted least-privilege use of the key via
# their own policies; it is the delegation the KMS default key policy uses, and
# dropping it would leave a key manageable only through its key policy, where a
# mistake locks the account out of the key.
#
# A key policy applies only to its own key, so "*" as its resource means that key. Each
# statement names the key's ARN instead, which KMS accepts and which says the same thing
# explicitly. That is why the policies are attached with aws_kms_key_policy after the
# key exists: a key's policy cannot name the key's ARN while the key is being created.
data "aws_iam_policy_document" "root" {
  for_each = local.keys

  statement {
    sid       = "EnableIAMPolicies"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = [each.value.arn]
    principals {
      type        = "AWS"
      identifiers = [local.root_arn]
    }
  }
}

# Until this applies, a new key has the KMS default policy, which is the root statement
# above with "*" as its resource.
resource "aws_kms_key_policy" "this" {
  for_each = local.keys

  key_id = each.value.key_id
  policy = lookup(local.service_policies, each.key, data.aws_iam_policy_document.root[each.key].json)
}

# ---------------------------------------------------------------------------
# S3 data key — encrypts input/output/tiles/models buckets (SSE-KMS)
# ---------------------------------------------------------------------------
resource "aws_kms_key" "s3" {
  description             = "${var.prefix} S3 bucket encryption"
  deletion_window_in_days = var.deletion_window_in_days
  enable_key_rotation     = true

  tags = { Name = "${var.prefix}-s3" }
}

resource "aws_kms_alias" "s3" {
  name          = "alias/${var.prefix}-s3"
  target_key_id = aws_kms_key.s3.key_id
}

# ---------------------------------------------------------------------------
# SQS data key — encrypts job queue + DLQ. Must allow the SQS service principal
# to generate/decrypt data keys on our behalf (SQS encryption baseline).
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "sqs" {
  source_policy_documents = [data.aws_iam_policy_document.root["sqs"].json]

  statement {
    sid       = "AllowSQSService"
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.sqs.arn]
    principals {
      type        = "Service"
      identifiers = ["sqs.amazonaws.com"]
    }
    # @secure_recommendation: confused-deputy guard. SQS may use the key only for queues
    # in this account. The SQS key-management guide gives aws:SourceAccount as the
    # condition to use when the queue ARN is not known here; this module is applied
    # before the queues exist.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_kms_key" "sqs" {
  description             = "${var.prefix} SQS queue encryption"
  deletion_window_in_days = var.deletion_window_in_days
  enable_key_rotation     = true

  tags = { Name = "${var.prefix}-sqs" }
}

resource "aws_kms_alias" "sqs" {
  name          = "alias/${var.prefix}-sqs"
  target_key_id = aws_kms_key.sqs.key_id
}

# ---------------------------------------------------------------------------
# DynamoDB data key — encrypts the job store table. Access control is delegated to
# IAM (the root statement) so the backend/worker IRSA roles can use the key via their
# own least-privilege policies. DynamoDB uses the key transparently on our behalf.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "dynamodb" {
  description             = "${var.prefix} DynamoDB job store encryption"
  deletion_window_in_days = var.deletion_window_in_days
  enable_key_rotation     = true

  tags = { Name = "${var.prefix}-dynamodb" }
}

resource "aws_kms_alias" "dynamodb" {
  name          = "alias/${var.prefix}-dynamodb"
  target_key_id = aws_kms_key.dynamodb.key_id
}

# ---------------------------------------------------------------------------
# EKS secrets key — envelope-encrypts Kubernetes Secrets in etcd. The EKS cluster
# IAM role is granted use of this key via an inline policy in the eks module.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "eks" {
  description             = "${var.prefix} EKS secrets envelope encryption"
  deletion_window_in_days = var.deletion_window_in_days
  enable_key_rotation     = true

  tags = { Name = "${var.prefix}-eks-secrets" }
}

resource "aws_kms_alias" "eks" {
  name          = "alias/${var.prefix}-eks-secrets"
  target_key_id = aws_kms_key.eks.key_id
}

# ---------------------------------------------------------------------------
# Logs key — encrypts CloudWatch log groups (EKS control plane, VPC flow logs).
# Must allow the CloudWatch Logs regional service principal, scoped to log groups
# in this account/region via the encryption-context condition.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "logs" {
  source_policy_documents = [data.aws_iam_policy_document.root["logs"].json]

  # kms:DescribeKey used to be listed here too. A DescribeKey request carries no
  # encryption context, so under this statement's condition it could never be allowed:
  # it was dead text that read as a grant without one. It is removed rather than moved
  # to a statement of its own, since nothing needed it. The principal that associates
  # the key with a log group needs DescribeKey, and that is the Terraform caller,
  # through the root statement.
  statement {
    sid    = "AllowCloudWatchLogs"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
    ]
    resources = [aws_kms_key.logs.arn]
    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }
    # StringLike, not ArnLike: the encryption-context key is a string key whose value
    # happens to be an ARN, and the policy validator flags an ARN operator on it. The
    # wildcard match is the same.
    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:*"]
    }
  }
}

resource "aws_kms_key" "logs" {
  description             = "${var.prefix} CloudWatch Logs encryption"
  deletion_window_in_days = var.deletion_window_in_days
  enable_key_rotation     = true

  tags = { Name = "${var.prefix}-logs" }
}

resource "aws_kms_alias" "logs" {
  name          = "alias/${var.prefix}-logs"
  target_key_id = aws_kms_key.logs.key_id
}

# ---------------------------------------------------------------------------
# ECR key — encrypts the image repositories (encryption_type = KMS). Access control is
# delegated to IAM (the root statement). ECR adds its own grants on this key when a repository
# is created, using the creating principal's kms:CreateGrant, and retires them when
# the repository is deleted, so nodes pulling images need no KMS permission of their
# own.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "ecr" {
  description             = "${var.prefix} ECR image encryption"
  deletion_window_in_days = var.deletion_window_in_days
  enable_key_rotation     = true

  tags = { Name = "${var.prefix}-ecr" }
}

resource "aws_kms_alias" "ecr" {
  name          = "alias/${var.prefix}-ecr"
  target_key_id = aws_kms_key.ecr.key_id
}

# ---------------------------------------------------------------------------
# Alarm topic key — encrypts the SNS topic the CloudWatch alarms publish to. Created
# only when that topic is (var.create_sns_key follows alarm_email).
#
# The AWS managed key alias/aws/sns cannot be used here: its key policy cannot be
# changed, and an alarm action publishes as cloudwatch.amazonaws.com, which needs
# kms:GenerateDataKey* and kms:Decrypt on the topic's key. The SNS guide for AWS
# service event sources therefore requires a customer managed key with the statement
# below, and recommends the aws:SourceAccount guard it carries.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "sns" {
  count = var.create_sns_key ? 1 : 0

  # The root statement, as for every other key.
  statement {
    sid       = "EnableIAMPolicies"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = [aws_kms_key.sns[0].arn]
    principals {
      type        = "AWS"
      identifiers = [local.root_arn]
    }
  }

  statement {
    sid       = "AllowAlarmPublishers"
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = [aws_kms_key.sns[0].arn]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com", "sns.amazonaws.com"]
    }
    # @secure_recommendation: confused-deputy guard, so another account's alarm cannot
    # have CloudWatch use this key on its behalf.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_kms_key" "sns" {
  count = var.create_sns_key ? 1 : 0

  description             = "${var.prefix} alarm SNS topic encryption"
  deletion_window_in_days = var.deletion_window_in_days
  enable_key_rotation     = true

  tags = { Name = "${var.prefix}-sns" }
}

resource "aws_kms_alias" "sns" {
  count = var.create_sns_key ? 1 : 0

  name          = "alias/${var.prefix}-sns"
  target_key_id = aws_kms_key.sns[0].key_id
}

resource "aws_kms_key_policy" "sns" {
  count = var.create_sns_key ? 1 : 0

  key_id = aws_kms_key.sns[0].key_id
  policy = data.aws_iam_policy_document.sns[0].json
}
