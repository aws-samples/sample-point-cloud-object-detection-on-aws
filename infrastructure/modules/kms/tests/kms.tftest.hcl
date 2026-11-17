# Plan-only checks of the key policies. The AWS provider is mocked, so this needs no
# credentials and creates nothing:
#
#   terraform -chdir=infrastructure/modules/kms init -backend=false
#   terraform -chdir=infrastructure/modules/kms test

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = {
      region = "us-east-1"
    }
  }
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
  # A key's ARN is known only once it exists; the mock makes it known at plan, so the
  # key policy assertions below can compare against it.
  mock_resource "aws_kms_key" {
    override_during = plan
    defaults = {
      arn    = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000"
      key_id = "00000000-0000-0000-0000-000000000000"
    }
  }
}

variables {
  prefix = "pointcloud-detection-dev"
}

run "logs_statement_is_bound_to_this_accounts_log_groups" {
  command = plan

  assert {
    # DescribeKey carries no encryption context, so under this condition it could
    # never be allowed; it is not listed.
    condition     = !contains(data.aws_iam_policy_document.logs.statement[0].actions, "kms:DescribeKey")
    error_message = "the CloudWatch Logs statement must not list kms:DescribeKey"
  }
  assert {
    condition = anytrue([
      for c in data.aws_iam_policy_document.logs.statement[0].condition :
      c.test == "StringLike" && c.variable == "kms:EncryptionContext:aws:logs:arn" && c.values == tolist(["arn:aws:logs:us-east-1:${data.aws_caller_identity.current.account_id}:log-group:*"])
    ])
    error_message = "the encryption-context condition must use StringLike on this account's log groups"
  }
}

run "service_principals_carry_source_account" {
  command = plan

  variables {
    create_sns_key = true
  }

  assert {
    condition = anytrue([
      for c in data.aws_iam_policy_document.sqs.statement[0].condition :
      c.test == "StringEquals" && c.variable == "aws:SourceAccount" && c.values == tolist([data.aws_caller_identity.current.account_id])
    ])
    error_message = "the sqs.amazonaws.com grant must be conditioned on aws:SourceAccount"
  }
  assert {
    condition = data.aws_iam_policy_document.sns[0].statement[1].principals == toset([{
      type        = "Service"
      identifiers = toset(["cloudwatch.amazonaws.com", "sns.amazonaws.com"])
    }])
    error_message = "the alarm topic key must admit the CloudWatch and SNS service principals"
  }
  assert {
    condition = data.aws_iam_policy_document.sns[0].statement[1].actions == toset(["kms:GenerateDataKey*", "kms:Decrypt"]) && anytrue([
      for c in data.aws_iam_policy_document.sns[0].statement[1].condition :
      c.test == "StringEquals" && c.variable == "aws:SourceAccount" && c.values == tolist([data.aws_caller_identity.current.account_id])
    ])
    error_message = "the alarm topic key grants GenerateDataKey* and Decrypt, conditioned on aws:SourceAccount"
  }
}

run "sns_key_only_with_a_topic" {
  command = plan

  assert {
    condition     = length(aws_kms_key.sns) == 0 && output.sns_key_arn == null
    error_message = "no alarm topic, no alarm topic key"
  }
  assert {
    condition     = aws_kms_key.ecr.enable_key_rotation
    error_message = "the ECR key must rotate"
  }
}

run "sns_key_when_asked" {
  command = plan

  variables {
    create_sns_key = true
  }

  assert {
    condition     = length(aws_kms_key.sns) == 1 && aws_kms_key.sns[0].enable_key_rotation
    error_message = "create_sns_key must create a rotating alarm topic key"
  }
}

run "every_key_policy_names_its_own_key" {
  command = plan

  variables {
    create_sns_key = true
  }

  assert {
    condition     = length(aws_kms_key_policy.this) == 6 && length(aws_kms_key_policy.sns) == 1
    error_message = "every key, the alarm topic key included, must have its own key policy"
  }
  assert {
    # Not "*": each statement names the key the policy is attached to.
    condition = alltrue([
      for k, d in data.aws_iam_policy_document.root :
      alltrue([for st in d.statement : st.resources == toset([local.keys[k].arn])])
    ])
    error_message = "the root statement of each key policy must name that key's ARN"
  }
  assert {
    condition = alltrue([
      for d in [data.aws_iam_policy_document.sqs, data.aws_iam_policy_document.logs, data.aws_iam_policy_document.sns[0]] :
      alltrue([for st in d.statement : !contains(tolist(st.resources), "*")])
    ])
    error_message = "no statement may use \"*\" as its resource"
  }
  assert {
    condition     = aws_kms_key_policy.this["s3"].key_id == aws_kms_key.s3.key_id && aws_kms_key_policy.sns[0].key_id == aws_kms_key.sns[0].key_id
    error_message = "each key policy must be attached to its own key"
  }
}

run "no_alarm_key_no_alarm_policy" {
  command = plan

  assert {
    condition     = length(aws_kms_key_policy.sns) == 0 && length(data.aws_iam_policy_document.sns) == 0
    error_message = "without the alarm topic key there is no policy for it"
  }
}
