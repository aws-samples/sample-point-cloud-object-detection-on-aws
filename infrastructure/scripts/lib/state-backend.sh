# shellcheck shell=bash
# Sourced by deploy.sh and destroy.sh: the `terraform init` arguments for the remote
# state (backend.tf), so that the state is encrypted with the state bucket's own key
# (THREAT_MODEL.md T62, A7).
#
# The bucket's default encryption applies only to objects written without an
# encryption header, and the S3 backend sends one: with `encrypt = true` and no
# `kms_key_id`, it asks for SSE-S3 on every write. The state, and with it the Cognito
# client secret in public mode, would then be SSE-S3 whatever key bootstrap-state.sh
# set. Passing the bucket's default KMS key as `kms_key_id` makes every state write use
# that key. A bucket that does not default to SSE-KMS gets a warning, and its state
# stays SSE-S3; bootstrap-state.sh sets SSE-KMS.
#
# Adding `kms_key_id` changes the backend configuration, which a plain `terraform init`
# refuses in a directory initialised before ("Backend configuration changed"). When the
# saved configuration names the same bucket, key and Region, the state does not move,
# so -reconfigure is added. Any other change is left to Terraform, which stops and asks.

# state_backend_args <bucket> <key> <region> [infrastructure dir]
# Sets BACKEND_ARGS.
state_backend_args() {
  local bucket="$1" key="$2" region="$3" dir="${4:-.}" account alg kms
  local saved="${dir}/.terraform/terraform.tfstate"
  BACKEND_ARGS=(-backend-config="bucket=${bucket}" -backend-config="key=${key}"
    -backend-config="region=${region}")
  account="$(aws sts get-caller-identity --query Account --output text)"
  read -r alg kms <<<"$(aws s3api get-bucket-encryption --bucket "${bucket}" \
    --expected-bucket-owner "${account}" --region "${region}" \
    --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.[SSEAlgorithm,KMSMasterKeyID]' \
    --output text 2>/dev/null || echo "None None")"
  case "${alg}" in
    aws:kms*)
      # No key id is the AWS managed key aws/s3. DescribeKey resolves an alias or key id
      # to the key's ARN.
      if [ -z "${kms}" ] || [ "${kms}" = "None" ]; then kms="alias/aws/s3"; fi
      kms="$(aws kms describe-key --key-id "${kms}" --region "${region}" \
        --query KeyMetadata.Arn --output text)"
      BACKEND_ARGS+=(-backend-config="kms_key_id=${kms}")
      ;;
    *)
      echo "warning: s3://${bucket} does not default to SSE-KMS, so Terraform writes its state with SSE-S3. Re-run scripts/bootstrap-state.sh on it to set SSE-KMS." >&2
      ;;
  esac
  [ -f "${saved}" ] || return 0
  if ! command -v jq >/dev/null 2>&1; then
    echo "warning: jq not found, so the saved backend configuration cannot be compared. If terraform init reports \"Backend configuration changed\" and the state's bucket, key and Region are unchanged, re-run it with -reconfigure." >&2
  elif jq -e --arg b "${bucket}" --arg k "${key}" --arg r "${region}" \
      '.backend.type == "s3" and .backend.config.bucket == $b and .backend.config.key == $k and .backend.config.region == $r' \
      "${saved}" >/dev/null 2>&1; then
    BACKEND_ARGS+=(-reconfigure)
  fi
}
