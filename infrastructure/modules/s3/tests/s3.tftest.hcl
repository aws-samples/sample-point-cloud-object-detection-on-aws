# Plan-only checks of the bucket controls. The AWS provider is mocked, so this needs no
# credentials and creates nothing:
#
#   terraform -chdir=infrastructure/modules/s3 init -backend=false
#   terraform -chdir=infrastructure/modules/s3 test

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      # A 12-digit id, because the_longest_region_fits checks a bucket name at exactly
      # 63 characters; the mock's random default is 8. 123456789012 is the placeholder
      # the AWS documentation uses, not a real account; the assertions read it back
      # from this data source rather than repeat it.
      account_id = "123456789012"
    }
  }
  mock_data "aws_region" {
    defaults = {
      region = "us-east-1"
    }
  }
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
  # Bucket ARNs are known at plan time in practice (arn:aws:s3:::<name>); the mock
  # makes them so for the policy assertions below.
  mock_resource "aws_s3_bucket" {
    override_during = plan
    defaults = {
      arn = "arn:aws:s3:::mock-bucket"
    }
  }
}

variables {
  prefix      = "pointcloud-detection-dev"
  environment = "dev"
  kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
}

run "account_block_public_access_is_opt_in" {
  command = plan

  assert {
    # The account setting is the adopter's, not the stack's: not managed by default.
    condition     = length(aws_s3_account_public_access_block.account_opt_in) == 0
    error_message = "the account-level public access block must be off by default"
  }
  assert {
    # Every bucket keeps its own block regardless.
    condition = alltrue([
      for b in aws_s3_bucket_public_access_block.buckets :
      b.block_public_acls && b.block_public_policy && b.ignore_public_acls && b.restrict_public_buckets
    ]) && length(aws_s3_bucket_public_access_block.buckets) == 4
    error_message = "every data bucket must block public access"
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.logs.block_public_policy && aws_s3_bucket_public_access_block.logs.restrict_public_buckets
    error_message = "the access-logs bucket must block public access"
  }
}

run "account_block_public_access_when_asked" {
  command = plan

  variables {
    manage_account_public_access_block = true
  }

  assert {
    condition = length(aws_s3_account_public_access_block.account_opt_in) == 1 && alltrue([
      aws_s3_account_public_access_block.account_opt_in[0].block_public_acls,
      aws_s3_account_public_access_block.account_opt_in[0].block_public_policy,
      aws_s3_account_public_access_block.account_opt_in[0].ignore_public_acls,
      aws_s3_account_public_access_block.account_opt_in[0].restrict_public_buckets,
    ])
    error_message = "opting in must set all four account-level flags"
  }
}

run "access_log_delivery_is_bound_to_this_stack" {
  command = plan

  assert {
    condition = anytrue([
      for c in data.aws_iam_policy_document.logs.statement[0].condition :
      c.test == "StringEquals" && c.variable == "aws:SourceAccount" && c.values == tolist([data.aws_caller_identity.current.account_id])
    ])
    error_message = "logging.s3.amazonaws.com must be conditioned on aws:SourceAccount"
  }
  assert {
    condition = anytrue([
      for c in data.aws_iam_policy_document.logs.statement[0].condition :
      c.test == "ArnLike" && c.variable == "aws:SourceArn" && length(c.values) == 4
    ])
    error_message = "logging.s3.amazonaws.com must be conditioned on the four data buckets' ARNs"
  }
}

run "versions_and_models_expire" {
  command = plan

  assert {
    condition     = aws_s3_bucket_lifecycle_configuration.logs.rule[0].noncurrent_version_expiration[0].noncurrent_days > 0
    error_message = "the versioned access-logs bucket must expire noncurrent versions"
  }
  assert {
    # A lifecycle rule on every data bucket, including models.
    condition     = toset(keys(aws_s3_bucket_lifecycle_configuration.buckets)) == toset(["input", "output", "tiles", "models"])
    error_message = "every data bucket needs a lifecycle rule"
  }
  assert {
    # Old checkpoints go; the current one never does.
    condition     = length(aws_s3_bucket_lifecycle_configuration.buckets["models"].rule[0].expiration) == 0 && aws_s3_bucket_lifecycle_configuration.buckets["models"].rule[0].noncurrent_version_expiration[0].noncurrent_days > 0
    error_message = "the models bucket must expire noncurrent versions only"
  }
  assert {
    condition = alltrue([
      for k in ["input", "output", "tiles"] :
      aws_s3_bucket_lifecycle_configuration.buckets[k].rule[0].expiration[0].days > 0 && aws_s3_bucket_lifecycle_configuration.buckets[k].rule[0].noncurrent_version_expiration[0].noncurrent_days == 7
    ])
    error_message = "data buckets keep their expiry and 7-day noncurrent expiry"
  }
}

run "buckets_are_in_the_account_regional_namespace" {
  command = plan

  assert {
    # A name in the shared global namespace can be registered by anyone who guesses it.
    condition = alltrue([
      for b in aws_s3_bucket.buckets : b.bucket_namespace == "account-regional" && endswith(b.bucket, "-${data.aws_caller_identity.current.account_id}-us-east-1-an")
    ]) && aws_s3_bucket.buckets["input"].bucket == "pointcloud-detection-dev-input-${data.aws_caller_identity.current.account_id}-us-east-1-an"
    error_message = "every data bucket must be in the account regional namespace"
  }
  assert {
    condition     = aws_s3_bucket.logs.bucket_namespace == "account-regional" && aws_s3_bucket.logs.bucket == "pointcloud-detection-dev-logs-${data.aws_caller_identity.current.account_id}-us-east-1-an"
    error_message = "the access-logs bucket must be in the account regional namespace"
  }
}

run "the_longest_region_fits" {
  command = plan

  variables {
    prefix = "pointcloud-detection-prod"
  }

  override_data {
    target = data.aws_region.current
    values = {
      region = "ap-southeast-2"
    }
  }

  assert {
    condition     = length(aws_s3_bucket.buckets["models"].bucket) <= 63
    error_message = "the default names must fit S3's limit in a 14-character Region"
  }
}

run "an_over_long_bucket_name_is_refused" {
  command = plan

  variables {
    prefix = "a-project-name-long-enough-to-overflow-the-limit-dev"
  }

  expect_failures = [aws_s3_bucket.logs, aws_s3_bucket.buckets]
}

run "a_region_without_the_namespace_is_refused" {
  command = plan

  override_data {
    target = data.aws_region.current
    values = {
      region = "me-south-1"
    }
  }

  expect_failures = [aws_s3_bucket.logs]
}
