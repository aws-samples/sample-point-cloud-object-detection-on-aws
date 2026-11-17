# Plan-only checks of the image repositories. The AWS provider is mocked, so this needs
# no credentials and creates nothing:
#
#   terraform -chdir=infrastructure/modules/ecr init -backend=false
#   terraform -chdir=infrastructure/modules/ecr test

mock_provider "aws" {}

variables {
  prefix      = "pointcloud-detection"
  kms_key_arn = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000001"
}

run "repositories_are_kms_encrypted_and_immutable" {
  command = plan

  assert {
    condition = alltrue([
      for r in aws_ecr_repository.repos :
      r.encryption_configuration[0].encryption_type == "KMS" && r.encryption_configuration[0].kms_key == "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000001"
    ]) && length(aws_ecr_repository.repos) == 3
    error_message = "every repository must use SSE-KMS with the project CMK"
  }
  assert {
    condition = alltrue([
      for r in aws_ecr_repository.repos : r.image_tag_mutability == "IMMUTABLE" && r.image_scanning_configuration[0].scan_on_push
    ])
    error_message = "tags stay immutable and images are scanned on push"
  }
}
