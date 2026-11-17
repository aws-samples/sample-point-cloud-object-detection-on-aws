# Remote state.
#
# There was no backend block here, so state was local: a terraform.tfstate file in this
# directory, gitignored. Three consequences, all of which were being hit:
#
#   1. State lived on one operator's machine. A second person running `terraform apply`
#      started from an empty state and tried to CREATE resources that already existed,
#      which fails on globally-unique names (S3 buckets, IAM roles) and half-applies
#      everything else.
#   2. No locking, so two concurrent applies could interleave writes and corrupt state.
#   3. Losing that machine orphaned every resource, with no record of what existed.
#
# This is a PARTIAL configuration on purpose — bucket, key and region are supplied at
# init time rather than hardcoded, so the same config serves more than one environment
# and the repository carries no account-specific value:
#
#   terraform init \
#     -backend-config="bucket=<state-bucket>" \
#     -backend-config="key=pointcloud-detection/<env>/terraform.tfstate" \
#     -backend-config="region=<region>" \
#     -backend-config="kms_key_id=<the state bucket's default KMS key ARN>"
#
# `scripts/bootstrap-state.sh` creates a suitable bucket. `scripts/deploy.sh` passes
# these from TF_STATE_BUCKET / TF_STATE_REGION, and reads the key from the bucket
# (scripts/lib/state-backend.sh). Without `kms_key_id`, `encrypt = true` writes the
# state with SSE-S3, overriding the bucket's SSE-KMS default.
#
# Locking uses the S3 backend's native lock file (`use_lockfile`), which needs no
# DynamoDB table and no extra IAM. It requires Terraform >= 1.10, which is why
# versions.tf pins that floor.
#
# CI is unaffected: `terraform init -backend=false` skips backend initialisation
# entirely, which is what the pipeline's validate job uses.
#
# Migrating an existing local state into a bucket:
#   terraform init -migrate-state -backend-config=...
# Terraform will show what it is copying and ask before writing. Keep the local
# terraform.tfstate until a subsequent `terraform plan` reports no changes.
terraform {
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
}
