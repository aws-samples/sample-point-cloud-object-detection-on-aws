# Plan-only checks of the flow-log role. The AWS provider is mocked, so this needs no
# credentials and creates nothing:
#
#   terraform -chdir=infrastructure/modules/vpc init -backend=false
#   terraform -chdir=infrastructure/modules/vpc test

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
}

variables {
  project_name       = "pointcloud-detection"
  environment        = "dev"
  vpc_cidr           = "10.0.0.0/16"
  availability_zones = ["us-east-1a", "us-east-1b"]
  kms_key_arn        = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000001"
}

run "flow_log_role_trust_has_source_conditions" {
  command = plan

  assert {
    condition = anytrue([
      for c in data.aws_iam_policy_document.flow_assume.statement[0].condition :
      c.test == "StringEquals" && c.variable == "aws:SourceAccount" && c.values == tolist([data.aws_caller_identity.current.account_id])
    ])
    error_message = "vpc-flow-logs.amazonaws.com must be conditioned on aws:SourceAccount"
  }
  assert {
    condition = anytrue([
      for c in data.aws_iam_policy_document.flow_assume.statement[0].condition :
      c.test == "ArnLike" && c.variable == "aws:SourceArn" && c.values == tolist(["arn:aws:ec2:us-east-1:${data.aws_caller_identity.current.account_id}:vpc-flow-log/*"])
    ])
    error_message = "vpc-flow-logs.amazonaws.com must be conditioned on this account's flow-log ARNs"
  }
}
