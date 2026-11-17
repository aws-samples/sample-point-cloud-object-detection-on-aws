# Plan-only checks of the alarm topic. The AWS provider is mocked, so this needs no
# credentials and creates nothing:
#
#   terraform -chdir=infrastructure/modules/monitoring init -backend=false
#   terraform -chdir=infrastructure/modules/monitoring test

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = {
      region = "us-east-1"
    }
  }
}

variables {
  cluster_name = "pointcloud-detection-dev"
  queue_name   = "pointcloud-detection-dev-jobs"
  dlq_name     = "pointcloud-detection-dev-jobs-dlq"
  kms_key_arn  = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000001"
}

run "no_email_no_topic" {
  command = plan

  assert {
    condition     = length(aws_sns_topic.alarms) == 0 && output.sns_topic_arn == ""
    error_message = "no alarm_email, no topic"
  }
}

run "alarm_topic_is_encrypted_with_the_cmk" {
  command = plan

  variables {
    alarm_email     = "ops@example.com"
    sns_kms_key_arn = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000002"
  }

  assert {
    condition     = aws_sns_topic.alarms[0].kms_master_key_id == "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000002"
    error_message = "the alarm topic must use the customer managed key"
  }
}

run "alarm_topic_needs_a_key" {
  command = plan

  variables {
    alarm_email = "ops@example.com"
  }

  expect_failures = [aws_sns_topic.alarms]
}
