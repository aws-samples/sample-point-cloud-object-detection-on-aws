# @secure_recommendation: VPC Flow Logs capture network-level metadata for intrusion
# detection and incident investigation (logging security baseline). Logs are delivered to
# a KMS-encrypted CloudWatch log group.

resource "aws_cloudwatch_log_group" "flow" {
  name              = "/vpc/${var.project_name}-${var.environment}/flow-logs"
  retention_in_days = var.flow_log_retention_days
  kms_key_id        = var.kms_key_arn
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

data "aws_iam_policy_document" "flow_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
    # @secure_recommendation: confused-deputy guard from the flow-logs IAM role guide:
    # only flow logs owned by this account, in this Region, may assume the role. The
    # flow log's ID is not known until it exists, so the ARN ends in a wildcard.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:ec2:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:vpc-flow-log/*"]
    }
  }
}

resource "aws_iam_role" "flow" {
  name               = "${var.project_name}-${var.environment}-vpc-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_assume.json
}

data "aws_iam_policy_document" "flow" {
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.flow.arn}:*"]
  }
}

resource "aws_iam_role_policy" "flow" {
  name   = "${var.project_name}-${var.environment}-vpc-flow-logs"
  role   = aws_iam_role.flow.id
  policy = data.aws_iam_policy_document.flow.json
}

resource "aws_flow_log" "main" {
  vpc_id                   = aws_vpc.main.id
  traffic_type             = "ALL"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.flow.arn
  iam_role_arn             = aws_iam_role.flow.arn
  max_aggregation_interval = 600

  tags = {
    Name = "${var.project_name}-${var.environment}-flow-log"
  }
}
