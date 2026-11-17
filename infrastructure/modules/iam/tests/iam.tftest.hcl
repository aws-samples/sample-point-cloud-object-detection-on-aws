# Plan-only checks of the workload IRSA policies. The AWS provider is mocked, so this
# needs no credentials and creates nothing:
#
#   terraform -chdir=infrastructure/modules/iam init -backend=false
#   terraform -chdir=infrastructure/modules/iam test

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  cluster_name         = "pointcloud-detection-dev"
  oidc_provider_arn    = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
  oidc_issuer          = "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
  namespace            = "pointcloud-detection"
  input_bucket_arn     = "arn:aws:s3:::pointcloud-detection-dev-input-111122223333-us-east-1-an"
  output_bucket_arn    = "arn:aws:s3:::pointcloud-detection-dev-output-111122223333-us-east-1-an"
  tiles_bucket_arn     = "arn:aws:s3:::pointcloud-detection-dev-tiles-111122223333-us-east-1-an"
  models_bucket_arn    = "arn:aws:s3:::pointcloud-detection-dev-models-111122223333-us-east-1-an"
  queue_arn            = "arn:aws:sqs:us-east-1:111122223333:pointcloud-detection-dev-jobs"
  jobs_table_arn       = "arn:aws:dynamodb:us-east-1:111122223333:table/pointcloud-detection-dev-jobs"
  s3_kms_key_arn       = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000001"
  sqs_kms_key_arn      = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000002"
  dynamodb_kms_key_arn = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000003"
}

run "backend_can_erase_a_job_and_nothing_more" {
  command = plan

  assert {
    # Exactly the three prefixes the API reads and writes.
    condition = anytrue([
      for s in data.aws_iam_policy_document.backend.statement :
      s.actions == toset(["s3:DeleteObject"]) && s.resources == toset([
        "arn:aws:s3:::pointcloud-detection-dev-input-111122223333-us-east-1-an/uploads/*",
        "arn:aws:s3:::pointcloud-detection-dev-output-111122223333-us-east-1-an/results/*",
        "arn:aws:s3:::pointcloud-detection-dev-tiles-111122223333-us-east-1-an/tiles/*",
      ])
    ])
    error_message = "the backend needs s3:DeleteObject on uploads/, results/ and tiles/ only"
  }
  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.backend.statement :
      contains(s.actions, "dynamodb:DeleteItem") && s.resources == toset(["arn:aws:dynamodb:us-east-1:111122223333:table/pointcloud-detection-dev-jobs"])
    ])
    error_message = "the backend needs dynamodb:DeleteItem on the jobs table"
  }
  assert {
    # Deleting a version destroys history at once; erasure goes through lifecycle.
    condition = alltrue(flatten([
      for d in [data.aws_iam_policy_document.backend, data.aws_iam_policy_document.worker, data.aws_iam_policy_document.training] : [
        for s in d.statement : !contains(s.actions, "s3:DeleteObjectVersion") && !contains(s.actions, "s3:*") && !contains(s.actions, "dynamodb:*")
      ]
    ]))
    error_message = "no role may hold s3:DeleteObjectVersion or a service wildcard"
  }
  assert {
    condition = alltrue(flatten([
      for s in data.aws_iam_policy_document.worker.statement : [!contains(s.actions, "s3:DeleteObject"), !contains(s.actions, "dynamodb:DeleteItem")]
    ]))
    error_message = "the worker must not gain delete permissions"
  }
}
