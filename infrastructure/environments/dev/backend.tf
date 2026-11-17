# Not loaded: Terraform runs from infrastructure/, and the backend it uses is the
# partial S3 configuration in infrastructure/backend.tf. deploy.sh supplies the rest
# from TF_STATE_BUCKET and TF_STATE_REGION, with a key of
# pointcloud-detection/<env>/terraform.tfstate.
#
# The bucket is created by scripts/bootstrap-state.sh. Name it in your account regional
# namespace, -<account-id>-<region>-an, which no other account can create; a name in the
# shared global namespace can be registered first by someone else. For example:
#
#   ./scripts/bootstrap-state.sh "pointcloud-detection-tfstate-$(aws sts get-caller-identity --query Account --output text)-us-east-1-an" us-east-1
#
# The equivalent explicit init for this environment:
#
#   terraform init \
#     -backend-config="bucket=pointcloud-detection-tfstate-<account-id>-us-east-1-an" \
#     -backend-config="key=pointcloud-detection/dev/terraform.tfstate" \
#     -backend-config="region=us-east-1" \
#     -backend-config="kms_key_id=<the state bucket's default KMS key ARN>"
#
# deploy.sh reads that key from the bucket (scripts/lib/state-backend.sh).
#
# Locking uses the S3 lock file (use_lockfile in infrastructure/backend.tf), so no
# DynamoDB table is needed.
