#!/bin/bash
set -euo pipefail

# Point Cloud Detection — Teardown Script
# Usage: ./destroy.sh [environment]
#
# Asks twice: before anything is removed, and again after showing the Terraform destroy
# plan. AUTO_APPROVE=1 skips only the second question.
#
# Never deletes the account-wide S3 Block Public Access setting. It is removed from
# Terraform state before the destroy is planned, and left as it is in the account.
#
# Empties the stack's S3 buckets, every object version and delete marker, once the
# destroy plan is confirmed and before the destroy. They are versioned and not
# force-destroyed, and S3 refuses to delete a bucket that still holds a version.
#
# The Kubernetes cleanup runs through a kubeconfig of its own. With the private
# cluster endpoint alone (the default), start the SSM relay and set
# PCD_KUBE_RELAY_PORT to its local port (lib/kubeconfig.sh). SKIP_KUBE_CLEANUP=1
# skips that cleanup and goes straight to the Terraform destroy, for a cluster that
# cannot be reached at all.

ENV="${1:-dev}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck source=lib/kubeconfig.sh
. "${SCRIPT_DIR}/lib/kubeconfig.sh"
# shellcheck source=lib/state-backend.sh
. "${SCRIPT_DIR}/lib/state-backend.sh"

# Deletes every object version and delete marker in a bucket, one page of up to 1,000
# at a time (the DeleteObjects limit), and skips a bucket that is already gone. The CLI's
# own --query builds each batch, so this needs no jq, and the batch goes to S3 as a
# file, so no object key reaches a command line.
empty_bucket() {
  local bucket="$1" errors
  while :; do
    if ! aws s3api list-object-versions --bucket "${bucket}" \
      --expected-bucket-owner "${ACCOUNT_ID}" --no-paginate \
      --query '{Objects: [Versions[].{Key: Key, VersionId: VersionId}, DeleteMarkers[].{Key: Key, VersionId: VersionId}][] | [:1000], Quiet: `true`}' \
      --output json >"${WORK_DIR}/delete.json" 2>"${WORK_DIR}/s3.err"; then
      if grep -q NoSuchBucket "${WORK_DIR}/s3.err"; then
        echo "    ${bucket}: already gone"
        return 0
      fi
      cat "${WORK_DIR}/s3.err" >&2
      return 1
    fi
    if ! grep -q '"Key":' "${WORK_DIR}/delete.json"; then
      echo "    ${bucket}: empty"
      return 0
    fi
    errors="$(aws s3api delete-objects --bucket "${bucket}" \
      --expected-bucket-owner "${ACCOUNT_ID}" --delete "file://${WORK_DIR}/delete.json" \
      --query 'Errors[0].[Key, Code, Message]' --output text)"
    if [ -n "${errors}" ] && [ "${errors}" != "None" ]; then
      echo "error: S3 refused to delete an object in ${bucket}: ${errors}" >&2
      return 1
    fi
  done
}

echo "============================================"
echo "Point Cloud Detection — TEARDOWN"
echo "Environment: ${ENV}"
echo "============================================"
echo ""
echo "WARNING: This will destroy all infrastructure!"
echo "Every object in the stack's S3 buckets is deleted with it, earlier versions included."
echo "The Terraform destroy plan is shown, and confirmed again, before anything is removed."
read -r -p "Continue? (yes/no): " CONFIRM || CONFIRM=""
if [ "${CONFIRM}" != "yes" ]; then
  echo "Aborted."
  exit 0
fi

cd "${INFRA_DIR}"

# State is remote (see backend.tf), so the backend has to be initialised before destroy
# can read what exists. Checked here, before anything is removed, rather than left to
# Terraform.
if [ -z "${TF_STATE_BUCKET:-}" ]; then
  cat >&2 <<'MSG'
error: TF_STATE_BUCKET is not set, so Terraform cannot reach the state for this stack.

    export TF_STATE_BUCKET=<bucket-name>
    export TF_STATE_REGION=<region>   # defaults to us-east-1

Nothing has been removed yet.
MSG
  exit 1
fi

# The state is written with the state bucket's own KMS key (lib/state-backend.sh).
state_backend_args "${TF_STATE_BUCKET}" "pointcloud-detection/${ENV}/terraform.tfstate" \
  "${TF_STATE_REGION:-us-east-1}"
terraform init "${BACKEND_ARGS[@]}"

# The saved destroy plan holds every planned value in clear text, sensitive ones
# included, and the kubeconfig below belongs to this run alone. Both live in a private
# temporary directory that is removed however the script exits.
WORK_DIR="$(mktemp -d)"
chmod 0700 "${WORK_DIR}"
trap 'rm -rf "${WORK_DIR}"' EXIT

# Account-wide S3 Block Public Access (THREAT_MODEL.md T37). Destroying
# aws_s3_account_public_access_block switches Block Public Access off for every bucket
# in the account, including a setting the adopter had before this stack and one another
# environment relies on. Every instance of that type is removed from state first, so the
# destroy cannot touch it: module.s3.aws_s3_account_public_access_block.account_opt_in[0]
# and the unconditional module.s3.aws_s3_account_public_access_block.account of earlier
# versions. A failure to list state is fatal, since no listing would mean no guard.
STATE_LIST_RC=0
STATE_LIST="$(terraform state list 2>&1)" || STATE_LIST_RC=$?
if [ "${STATE_LIST_RC}" -ne 0 ]; then
  case "${STATE_LIST}" in
    *"No state file was found"*) STATE_LIST="" ;;
    *)
      echo "${STATE_LIST}" >&2
      echo "error: could not list the Terraform state." >&2
      exit 1
      ;;
  esac
fi
BPA_ADDRESSES="$(printf '%s\n' "${STATE_LIST}" \
  | grep -E '(^|\.)aws_s3_account_public_access_block\.' \
  | grep -v -E '(^|\.)data\.aws_s3_account_public_access_block\.' || true)"
if [ -n "${BPA_ADDRESSES}" ]; then
  cat <<'MSG'
>>> Keeping the account's S3 Block Public Access setting.
    It is one setting for the whole account, not part of this stack alone: every bucket
    in the account relies on it, other stacks may too, and it may predate this one. It
    is removed from Terraform state only, and stays as it is in the account.
MSG
  while IFS= read -r address; do
    terraform state rm "${address}"
  done <<<"${BPA_ADDRESSES}"
fi

# Destroy removes everything in state whatever these say, but they still have to pass
# validation, and public mode requires a hostname. Reuse the values the stack was
# applied with; a state from before access modes existed has neither output, and
# port-forward needs no hostname.
APPLIED_MODE=$(terraform output -raw access_mode 2>/dev/null || true)
# An empty state has no outputs, and `terraform output -raw` then succeeds with none.
APPLIED_MODE="${APPLIED_MODE:-port-forward}"
APPLIED_HOST=$(terraform output -raw app_host 2>/dev/null || true)

# The Kubernetes cleanup below runs against this stack's cluster, named by its state,
# through a kubeconfig of its own. Using whatever context kubectl was last pointed at
# could remove another environment's releases and namespace.
CLUSTER_NAME=$(terraform output -raw cluster_name 2>/dev/null || true)
CLUSTER_REGION=$(terraform output -raw cluster_region 2>/dev/null || true)
KUBE_CLEANUP=""
if [ "${SKIP_KUBE_CLEANUP:-}" = "1" ]; then
  cat >&2 <<'MSG'
warning: SKIP_KUBE_CLEANUP=1: the Helm releases and namespaces are not removed first.
In public mode the ALB that the Load Balancer Controller made for the Ingress, and its
security groups and network interfaces, are then left behind, and the VPC destroy can
fail on them. Delete them in the EC2 console, then run this again.
MSG
elif [ -n "${CLUSTER_NAME}" ] && [ -n "${CLUSTER_REGION}" ]; then
  DESCRIBE_RC=0
  DESCRIBE=$(aws eks describe-cluster --name "${CLUSTER_NAME}" --region "${CLUSTER_REGION}" \
    --query cluster.status --output text 2>&1) || DESCRIBE_RC=$?
  if [ "${DESCRIBE_RC}" -eq 0 ]; then
    KUBE_CLEANUP=1
  elif [[ "${DESCRIBE}" == *ResourceNotFoundException* ]]; then
    echo "[i] cluster ${CLUSTER_NAME} no longer exists; skipping the Kubernetes cleanup."
  else
    echo "${DESCRIBE}" >&2
    echo "error: could not look up cluster ${CLUSTER_NAME}." >&2
    exit 1
  fi
fi

if [ -n "${KUBE_CLEANUP}" ]; then
  export KUBECONFIG="${WORK_DIR}/kubeconfig"
  write_kubeconfig "${CLUSTER_NAME}" "${CLUSTER_REGION}" "${KUBECONFIG}"
  # Cluster access is an EKS access entry that Terraform manages, granting
  # cluster-admin to the identity that applies the stack. Nothing else has it, the
  # bastion included.
  if ! kubectl auth can-i '*' '*' --all-namespaces --request-timeout=30s >/dev/null 2>&1; then
    cat >&2 <<MSG
error: $(aws sts get-caller-identity --query Arn --output text 2>/dev/null || echo "this identity") cannot administer cluster ${CLUSTER_NAME}, or cannot reach it.

Run this script as an identity with a cluster-admin access entry, from a network that
can reach the cluster endpoint.
$(cluster_access_hint)
SKIP_KUBE_CLEANUP=1 destroys without the Kubernetes cleanup. Nothing has been removed
yet.
MSG
    exit 1
  fi
fi

# -detailed-exitcode: 0 means nothing to destroy, 2 means changes, anything else is an
# error.
PLAN_RC=0
terraform plan -destroy -var-file="environments/${ENV}/terraform.tfvars" \
  -var "access_mode=${APPLIED_MODE}" \
  -var "app_host=${APPLIED_HOST}" \
  -detailed-exitcode \
  -out="${WORK_DIR}/tfplan" || PLAN_RC=$?
case "${PLAN_RC}" in
  0 | 2) ;;
  *) exit "${PLAN_RC}" ;;
esac

# The stack's buckets, named from state. The plan deletes them, and S3 refuses to delete
# one that holds any object version: a used stack's destroy stopped on BucketNotEmpty
# until they were emptied first (found on a sandbox cluster).
BUCKETS=""
if [ "${PLAN_RC}" -eq 2 ]; then
  BUCKET_ADDRESSES="$(printf '%s\n' "${STATE_LIST}" \
    | grep -E '(^|\.)aws_s3_bucket\.' \
    | grep -v -E '(^|\.)data\.aws_s3_bucket\.' || true)"
  while IFS= read -r address; do
    [ -n "${address}" ] || continue
    BUCKETS+="$(terraform state show -no-color "${address}" \
      | sed -n -E 's/^ *bucket *= *"([^"]+)"$/\1/p')"$'\n'
  done <<<"${BUCKET_ADDRESSES}"
  BUCKETS="$(sed '/^$/d' <<<"${BUCKETS}")"
  if [ -n "${BUCKETS}" ]; then
    echo ">>> The destroy empties these buckets first, every object version and delete marker:"
    sed 's/^/    /' <<<"${BUCKETS}"
  fi
fi

if [ "${PLAN_RC}" -eq 2 ]; then
  if [ "${AUTO_APPROVE:-}" = "1" ]; then
    echo "AUTO_APPROVE=1: destroying without asking again."
  else
    read -r -p "Destroy everything in the plan above? Only 'yes' is accepted: " CONFIRM || CONFIRM=""
    if [ "${CONFIRM}" != "yes" ]; then
      echo "Aborted; nothing was removed. AUTO_APPROVE=1 skips this question." >&2
      if [ -n "${BPA_ADDRESSES}" ]; then
        echo "The Block Public Access setting is out of Terraform state (see above) and unchanged in the account." >&2
      fi
      exit 1
    fi
  fi
fi

if [ -n "${KUBE_CLEANUP}" ]; then
  # Remove Helm releases first
  echo ">>> Removing Helm releases..."
  helm uninstall pointcloud-backend -n pointcloud-detection 2>/dev/null || true
  helm uninstall pointcloud-frontend -n pointcloud-detection 2>/dev/null || true
  # The Load Balancer Controller deletes the ALB and then clears the Ingress finalizers.
  # Removing the controller before that leaves the Ingresses stuck, so the namespace
  # delete below hangs, and the ALB's network interfaces stay in the subnets and fail
  # the VPC destroy. No Ingress at all (port-forward mode) returns at once.
  kubectl -n pointcloud-detection wait --for=delete ingress --all --timeout=5m 2>/dev/null || true
  helm uninstall cluster-autoscaler -n kube-system 2>/dev/null || true
  helm uninstall aws-load-balancer-controller -n kube-system 2>/dev/null || true
  helm uninstall nvidia-device-plugin -n kube-system 2>/dev/null || true
  # After the backend release, so the ScaledObject is gone before its CRD is.
  # Removing KEDA first leaves the CR orphaned and its finalizer blocks the
  # namespace delete below.
  helm uninstall keda -n keda 2>/dev/null || true
  kubectl delete namespace keda --ignore-not-found

  # Delete namespace
  echo ">>> Deleting namespace..."
  kubectl delete namespace pointcloud-detection --ignore-not-found
fi

# After the Kubernetes cleanup, so no worker writes to a bucket while it is emptied.
if [ -n "${BUCKETS}" ]; then
  echo ">>> Emptying the buckets..."
  ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
  while IFS= read -r bucket; do
    if ! empty_bucket "${bucket}"; then
      echo "error: could not empty ${bucket}, and the Terraform destroy has not started. The buckets listed before it are empty, and it may be partly emptied." >&2
      exit 1
    fi
  done <<<"${BUCKETS}"
fi

# Terraform destroy, of exactly the plan shown above.
if [ "${PLAN_RC}" -eq 2 ]; then
  echo ">>> Destroying Terraform infrastructure..."
  APPLY_RC=0
  terraform apply "${WORK_DIR}/tfplan" || APPLY_RC=$?
  if [ "${APPLY_RC}" -ne 0 ]; then
    cat >&2 <<'MSG'
error: the destroy stopped part-way. Run destroy.sh again: it plans what is left, empties
any bucket still in it, and asks again before it removes anything. The logs bucket can
fail with BucketNotEmpty when S3 delivers access logs into it after it was emptied.
MSG
    exit "${APPLY_RC}"
  fi
else
  echo ">>> Nothing in the Terraform state to destroy."
fi

# Log groups the destroy leaves. EKS creates the control-plane group and Fluent Bit the
# workloads group, outside Terraform (THREAT_MODEL.md T40), and the flow-log service
# recreated the flow-log group from its last records a second after the destroy deleted
# it: all three were found on a sandbox cluster. They hold the cluster's logs, so each is
# listed with the command that deletes it, not deleted. The flow-log group is
# /vpc/<cluster name>/flow-logs (modules/vpc/flow-logs.tf).
if [ "${PLAN_RC}" -eq 2 ] && [ -n "${CLUSTER_NAME}" ] && [ -n "${CLUSTER_REGION}" ]; then
  LEFT=""
  for prefix in "/aws/eks/${CLUSTER_NAME}/" "/vpc/${CLUSTER_NAME}/"; do
    LEFT+="$(aws logs describe-log-groups --region "${CLUSTER_REGION}" \
      --log-group-name-prefix "${prefix}" --query 'logGroups[].logGroupName' \
      --output text 2>/dev/null || true)"$'\t'
  done
  LEFT="$(tr '\t' '\n' <<<"${LEFT}" | sed -e '/^$/d' -e '/^None$/d')"
  if [ -n "${LEFT}" ]; then
    echo ""
    echo ">>> Left in CloudWatch Logs, holding the cluster's logs. Delete each when you no longer need it:"
    while IFS= read -r group; do
      echo "    aws logs delete-log-group --region ${CLUSTER_REGION} --log-group-name ${group}"
    done <<<"${LEFT}"
  fi
fi

echo ""
echo "============================================"
echo "Teardown complete."
echo "============================================"
