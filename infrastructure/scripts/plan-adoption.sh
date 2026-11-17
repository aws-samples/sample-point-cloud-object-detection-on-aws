#!/bin/bash
# Report which declared resources Terraform would CREATE, and emit the import blocks
# needed to adopt them instead.
#
# READ ONLY. This script runs `terraform plan` and nothing else. It does not apply, does
# not import, and does not write to state. Its output is a file of `import` blocks for a
# human to review.
#
# Why this is needed: with local state (see backend.tf), resources were applied from a
# machine whose state is not shared. From any other machine Terraform sees an empty
# state and plans to create resources that already exist in the account. Applying that
# plan fails on globally-unique names and half-creates the rest. Adoption imports the
# existing resources so the plan converges to "no changes".
#
# Usage:
#   ./plan-adoption.sh [environment]        # default: dev
#
# Output:
#   adoption/imports.generated.tf   import blocks, one per resource to adopt
#   adoption/plan.txt               the plan as Terraform prints it, sensitive values
#                                   shown as (sensitive value)
#
# No JSON copy of the plan is kept. `terraform show -json` prints sensitive values in
# plain text, the Cognito app client secret among them in public mode, so it is streamed
# straight into lib/generate_imports.py. The binary plan holds the same values and lives
# in a private temporary directory that is removed however the script exits.
#
# Terraform 1.5+ `import` blocks are used rather than `terraform import` CLI calls
# because they go through plan: you see what each import would attach before any state
# is written, and the blocks are reviewable in a merge request.

set -euo pipefail

ENV="${1:-dev}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_DIR="$(dirname "${SCRIPT_DIR}")"
OUT_DIR="${INFRA_DIR}/adoption"

cd "${INFRA_DIR}"

VARFILE="environments/${ENV}/terraform.tfvars"
if [ ! -f "${VARFILE}" ]; then
  echo "error: no var file at ${VARFILE}" >&2
  exit 1
fi

mkdir -p "${OUT_DIR}"

# Earlier versions of this script left the JSON plan here, secrets in plain text, and
# the binary plan too if a run failed part-way.
for stale in "${OUT_DIR}/plan.json" "${OUT_DIR}/tfplan.binary"; do
  if [ -e "${stale}" ]; then
    echo "==> Removing ${stale}, left by an earlier version: it holds the plan's sensitive values"
    rm -f "${stale}"
  fi
done

PLAN_DIR="$(mktemp -d)"
chmod 0700 "${PLAN_DIR}"
trap 'rm -rf "${PLAN_DIR}"' EXIT

# access_mode and app_host have to pass validation (public mode needs the host), and
# they decide what the plan declares: the Cognito pool exists only in public mode. Use
# the values the stack was applied with, as destroy.sh does. A state from before access
# modes existed, or no state at all, has no such output, and is planned as port-forward,
# which declares no Cognito resources to import. For a stack applied in public mode
# from another machine, set ACCESS_MODE=public and INGRESS_HOST, as for deploy.sh.
# With no state at all, `terraform output -raw` prints nothing and still succeeds.
MODE="${ACCESS_MODE:-$(terraform output -raw access_mode 2>/dev/null || true)}"
MODE="${MODE:-port-forward}"
HOST="${INGRESS_HOST:-$(terraform output -raw app_host 2>/dev/null || true)}"

echo "==> Identity: $(aws sts get-caller-identity --query Arn --output text 2>/dev/null || echo 'UNRESOLVED')"
echo "==> Environment: ${ENV}"
echo "==> Access mode: ${MODE}${HOST:+ (app_host ${HOST})}"
echo ""
echo "==> Planning (read only, no apply)"

# -lock=false: this only reads. Taking the state lock would block a legitimate
# concurrent operator for no reason.
terraform plan \
  -var-file="${VARFILE}" \
  -var "access_mode=${MODE}" \
  -var "app_host=${HOST}" \
  -lock=false \
  -input=false \
  -out="${PLAN_DIR}/tfplan.binary" > "${OUT_DIR}/plan.txt" 2>&1 || {
    echo "plan failed; see ${OUT_DIR}/plan.txt" >&2
    tail -30 "${OUT_DIR}/plan.txt" >&2
    exit 1
  }

terraform show -json "${PLAN_DIR}/tfplan.binary" \
  | python3 "${SCRIPT_DIR}/lib/generate_imports.py" - "${OUT_DIR}/imports.generated.tf"

echo ""
echo "==> Next steps"
echo ""
echo "    1. Read ${OUT_DIR}/imports.generated.tf. Every block has a TODO where the"
echo "       real resource ID goes — the plan knows the address, not what exists in"
echo "       your account."
echo "    2. Fill in the IDs. Each block carries the ID format for its resource type"
echo "       and an aws CLI command that prints candidates."
echo "    3. Move it into place:  mv ${OUT_DIR}/imports.generated.tf ."
echo "    4. terraform plan -var-file=${VARFILE} -var access_mode=${MODE} -var app_host=${HOST}"
echo "       Each import should report 'will be imported' and no change. A resource"
echo "       showing an in-place update after import means the live resource differs"
echo "       from the config — reconcile that before applying."
echo "    5. Once the plan is clean, terraform apply with the same -var-file and -var flags."
echo "    6. Delete the import blocks. They are one-shot; leaving them in place makes"
echo "       every future plan re-check imports that already happened."
echo ""
echo "    Do not skip step 4. An apply that still plans to CREATE will fail on"
echo "    globally-unique names and partially create the rest."
