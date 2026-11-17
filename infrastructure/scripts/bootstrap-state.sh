#!/bin/bash
# Create the S3 bucket that holds Terraform state.
#
# Chicken-and-egg: the bucket that stores state cannot itself be managed by the state it
# stores, so it is created out of band here rather than in the Terraform config. This is
# the one resource that is deliberately not under Terraform management.
#
# Idempotent — safe to re-run. Each step checks current state before changing anything,
# so running it against a bucket it set up before reports and moves on. It refuses an
# existing bucket whose policy is anything other than the one it writes, rather than
# replace a policy someone else relies on (THREAT_MODEL.md T62).
#
# Usage:
#   ./bootstrap-state.sh <bucket-name> [region] [kms-key-arn]
#
#   kms-key-arn   a customer managed KMS key for default encryption. Without one the
#                 bucket uses SSE-KMS with the AWS managed key aws/s3.
#
# Optional:
#   STATE_ACCESS_LOG_BUCKET=<bucket>   turn on server access logging, delivered to that
#                                      bucket under <bucket-name>/. It must exist in this
#                                      account, use SSE-S3, and allow the S3 logging
#                                      service to write to it.
#
# Then:
#   export TF_STATE_BUCKET=<bucket-name>
#   export TF_STATE_REGION=<region>
#   ./deploy.sh dev

set -euo pipefail

BUCKET="${1:-}"
REGION="${2:-us-east-1}"
KMS_KEY="${3:-}"

if [ -z "${BUCKET}" ]; then
  echo "usage: $0 <bucket-name> [region] [kms-key-arn]" >&2
  echo "" >&2
  echo "Name it in your account regional namespace, which no other account can use:" >&2
  echo "  $0 pointcloud-detection-tfstate-\$(aws sts get-caller-identity --query Account --output text)-us-east-1-an us-east-1" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq is required (deploy.sh needs it too)." >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
# Every call on the bucket names the account that must own it, so nothing here can
# configure, or later trust, a bucket of the same name in another account.
OWNER=(--expected-bucket-owner "${ACCOUNT_ID}")

# A name ending in -<account>-<region>-an is created in this account's regional
# namespace, which S3 lets no other account create, before or after this bucket is
# deleted. Any other name is in the shared global namespace, where whoever registers it
# first owns it (T62).
NAMESPACE=()
case "${BUCKET}" in
  *"-${ACCOUNT_ID}-${REGION}-an") NAMESPACE=(--bucket-namespace account-regional) ;;
  *) echo "note: s3://${BUCKET} is not in this account's regional namespace; a name ending in -${ACCOUNT_ID}-${REGION}-an would be." >&2 ;;
esac

echo "==> Target: s3://${BUCKET} in ${REGION}"
echo "==> Identity: $(aws sts get-caller-identity --query Arn --output text)"
echo ""

# The one bucket policy this script writes: deny any request not made over TLS.
POLICY="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": ["arn:aws:s3:::${BUCKET}", "arn:aws:s3:::${BUCKET}/*"],
      "Condition": { "Bool": { "aws:SecureTransport": "false" } }
    }
  ]
}
JSON
)"

# --- bucket -----------------------------------------------------------------
HEAD_RC=0
HEAD_ERR="$(aws s3api head-bucket --bucket "${BUCKET}" "${OWNER[@]}" 2>&1 >/dev/null)" || HEAD_RC=$?
if [ "${HEAD_RC}" -eq 0 ]; then
  echo "[=] bucket already exists in this account"

  # Checked before anything on an existing bucket changes. A policy other than the one
  # below belongs to someone, and putting ours would replace it whole.
  CURRENT_POLICY_RC=0
  CURRENT_POLICY="$(aws s3api get-bucket-policy --bucket "${BUCKET}" "${OWNER[@]}" \
    --query Policy --output text 2>&1)" || CURRENT_POLICY_RC=$?
  if [ "${CURRENT_POLICY_RC}" -ne 0 ]; then
    case "${CURRENT_POLICY}" in
      *NoSuchBucketPolicy*) CURRENT_POLICY="" ;;
      *)
        echo "${CURRENT_POLICY}" >&2
        echo "error: could not read the bucket policy of ${BUCKET}." >&2
        exit 1
        ;;
    esac
  fi
  if [ -n "${CURRENT_POLICY}" ] \
      && [ "$(jq -cS . <<<"${CURRENT_POLICY}")" != "$(jq -cS . <<<"${POLICY}")" ]; then
    cat >&2 <<MSG
error: s3://${BUCKET} already has a bucket policy, and not the one this script writes.
Refusing to replace it: nothing has been changed.

Use a new bucket for Terraform state, or add this statement to the existing policy
yourself and re-run:
$(jq '.Statement[0]' <<<"${POLICY}")
MSG
    exit 1
  fi
else
  case "${HEAD_ERR}" in
    *"(404)"* | *"Not Found"*) ;;
    *)
      echo "${HEAD_ERR}" >&2
      echo "error: s3://${BUCKET} exists but is not usable from account ${ACCOUNT_ID}: another account owns it, or this identity cannot read it. Choose another name." >&2
      exit 1
      ;;
  esac
  echo "[+] creating bucket"
  # us-east-1 must not be given a LocationConstraint; every other region must.
  # ${NAMESPACE[@]+...} keeps an empty array safe under set -u in bash 3.2.
  if [ "${REGION}" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" \
      ${NAMESPACE[@]+"${NAMESPACE[@]}"} > /dev/null
  else
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" \
      ${NAMESPACE[@]+"${NAMESPACE[@]}"} \
      --create-bucket-configuration "LocationConstraint=${REGION}" > /dev/null
  fi
fi

# --- versioning -------------------------------------------------------------
# Not optional. Versioning is what makes a corrupted or truncated state file
# recoverable, and state corruption is precisely the failure this bucket guards.
current=$(aws s3api get-bucket-versioning --bucket "${BUCKET}" "${OWNER[@]}" \
  --query 'Status' --output text 2>/dev/null || echo "None")
if [ "${current}" = "Enabled" ]; then
  echo "[=] versioning already enabled"
else
  echo "[+] enabling versioning"
  aws s3api put-bucket-versioning --bucket "${BUCKET}" "${OWNER[@]}" \
    --versioning-configuration Status=Enabled
fi

# --- encryption -------------------------------------------------------------
# SSE-KMS, so reading state takes kms:Decrypt as well as s3:GetObject and every use of
# the key is in CloudTrail. The AWS managed key aws/s3 needs no key policy, which a key
# created by the stack whose state this holds could not provide before the state exists.
# Pass a customer managed key to control who may decrypt.
#
# Default encryption applies to objects written without an encryption header.
# Terraform's S3 backend sends one: with `encrypt = true` (backend.tf) and no
# `kms_key_id`, every state write asks for SSE-S3, which overrides this default. So
# deploy.sh and destroy.sh pass this key as `kms_key_id` (lib/state-backend.sh).
read -r current_alg current_key <<<"$(aws s3api get-bucket-encryption --bucket "${BUCKET}" "${OWNER[@]}" \
  --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.[SSEAlgorithm,KMSMasterKeyID]' \
  --output text 2>/dev/null || echo "None None")"
if [[ "${current_alg}" == aws:kms* ]] && { [ -z "${KMS_KEY}" ] || [ "${current_key}" = "${KMS_KEY}" ]; }; then
  [ "${current_key}" != "None" ] || current_key="aws/s3"
  echo "[=] SSE-KMS default encryption already configured (key: ${current_key})"
else
  echo "[+] enabling SSE-KMS default encryption (key: ${KMS_KEY:-aws/s3})"
  aws s3api put-bucket-encryption --bucket "${BUCKET}" "${OWNER[@]}" \
    --server-side-encryption-configuration "$(jq -cn --arg key "${KMS_KEY}" '{Rules: [{
      ApplyServerSideEncryptionByDefault: ({SSEAlgorithm: "aws:kms"}
        + (if $key == "" then {} else {KMSMasterKeyID: $key} end)),
      BucketKeyEnabled: true}]}')"
fi

# --- public access ----------------------------------------------------------
echo "[+] blocking all public access"
aws s3api put-public-access-block --bucket "${BUCKET}" "${OWNER[@]}" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# --- access logging (optional) ----------------------------------------------
if [ -n "${STATE_ACCESS_LOG_BUCKET:-}" ]; then
  echo "[+] server access logging to s3://${STATE_ACCESS_LOG_BUCKET}/${BUCKET}/"
  aws s3api put-bucket-logging --bucket "${BUCKET}" "${OWNER[@]}" \
    --bucket-logging-status "$(jq -cn --arg target "${STATE_ACCESS_LOG_BUCKET}" \
      --arg prefix "${BUCKET}/" '{LoggingEnabled: {TargetBucket: $target, TargetPrefix: $prefix}}')"
else
  echo "[i] server access logging is off; set STATE_ACCESS_LOG_BUCKET to turn it on"
fi

# --- TLS-only ---------------------------------------------------------------
if [ -n "${CURRENT_POLICY:-}" ]; then
  echo "[=] TLS-only bucket policy already applied"
else
  echo "[+] applying TLS-only bucket policy"
  aws s3api put-bucket-policy --bucket "${BUCKET}" "${OWNER[@]}" --policy "${POLICY}"
fi

echo ""
echo "==> Done."
echo ""
echo "    export TF_STATE_BUCKET=${BUCKET}"
echo "    export TF_STATE_REGION=${REGION}"
echo ""
echo "    If resources already exist from a local-state apply, adopt them before"
echo "    applying: see docs/TERRAFORM_STATE_AND_ADOPTION.md"
