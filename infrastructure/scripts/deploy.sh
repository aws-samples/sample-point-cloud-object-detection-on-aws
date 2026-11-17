#!/bin/bash
set -euo pipefail

# Point Cloud Detection — Full Deployment Script
# Usage: ./deploy.sh [environment]
#
# Shows the Terraform plan and asks before applying it. AUTO_APPROVE=1 applies without
# asking, for automation that has reviewed the plan some other way.
#
# kubectl and helm run through a kubeconfig of this script's own; yours is not
# changed. With the private cluster endpoint alone (the default), start the SSM relay
# once the stack exists and set PCD_KUBE_RELAY_PORT to its local port
# (lib/kubeconfig.sh).
#
# Images are tagged with the commit's short SHA (build-push.sh), so the images' sources
# must be committed or IMAGE_TAG set; environments/<env>/terraform.tfvars need not be. Redeploying a commit whose images are already in ECR builds
# and pushes nothing.

ENV="${1:-dev}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck source=lib/kubeconfig.sh
. "${SCRIPT_DIR}/lib/kubeconfig.sh"
# shellcheck source=lib/state-backend.sh
. "${SCRIPT_DIR}/lib/state-backend.sh"

echo "============================================"
echo "Point Cloud Detection — Deployment"
echo "Environment: ${ENV}"
echo "============================================"

# Step 1: Terraform Apply
echo ""
echo ">>> Step 1: Provisioning infrastructure with Terraform..."
cd "${INFRA_DIR}"

# State lives in S3 (see backend.tf). The bucket is supplied here rather than hardcoded
# in the config so one repository serves more than one account. Checked explicitly
# because a bare `terraform init` against a partial backend config prompts for the
# bucket on stdin, which in a script means it hangs rather than fails.
if [ -z "${TF_STATE_BUCKET:-}" ]; then
  cat >&2 <<'MSG'
error: TF_STATE_BUCKET is not set.

Terraform state for this stack is stored in S3. Create the bucket once:

    ./scripts/bootstrap-state.sh <bucket-name> [region]

then:

    export TF_STATE_BUCKET=<bucket-name>
    export TF_STATE_REGION=<region>   # defaults to us-east-1

If you previously applied with local state, adopt the existing resources before
running this script, or the apply will try to recreate them and fail on
globally-unique names. See docs/TERRAFORM_STATE_AND_ADOPTION.md
MSG
  exit 1
fi

# How users reach the application. Checked before anything is provisioned rather than
# after a 20-minute apply.
#
#   public (default)  https://INGRESS_HOST on an internet-facing ALB, with Cognito
#                     sign-in on every request. Needs a domain and an ACM certificate.
#   port-forward      No load balancer. Users and scripts reach the UI and API through
#                     `kubectl port-forward` with their own IAM identity.
ACCESS_MODE="${ACCESS_MODE:-public}"

# Who reaches what (THREAT_MODEL.md T31). Unset means the backend chart's default for
# the access mode:
#
#   per-user  each signed-in user reaches only their own jobs, uploads, results,
#             training and models. The default in public mode.
#   shared    every user reaches every job: one team whose members may all see each
#             other's scans. The default, and the only choice, in port-forward mode.
ACCESS_SCOPE="${ACCESS_SCOPE:-}"
case "${ACCESS_SCOPE}" in
  "" | per-user | shared) ;;
  *)
    echo "error: ACCESS_SCOPE must be per-user or shared, not '${ACCESS_SCOPE}'." >&2
    exit 1
    ;;
esac
if [ "${ACCESS_SCOPE}" = per-user ] && [ "${ACCESS_MODE}" = port-forward ]; then
  echo "error: ACCESS_SCOPE=per-user needs ACCESS_MODE=public: the API takes each user's identity from the ALB's sign-in, and a port-forward tunnel carries none." >&2
  exit 1
fi
# Shared access on the internet is a choice the operator makes knowingly, never one a
# variable left in a shell makes for them.
if [ "${ACCESS_SCOPE}" = shared ] && [ "${ACCESS_MODE}" = public ]; then
  echo "warning: ACCESS_SCOPE=shared: every signed-in user will be able to list, open and delete every other user's jobs (THREAT_MODEL.md T31)." >&2
  if [ "${AUTO_APPROVE:-}" = "1" ]; then
    echo "AUTO_APPROVE=1: deploying public mode with shared access without asking."
  else
    read -r -p "Deploy public mode with shared access? Only 'yes' is accepted: " CONFIRM_SHARED || CONFIRM_SHARED=""
    if [ "${CONFIRM_SHARED}" != "yes" ]; then
      echo "Aborted. Unset ACCESS_SCOPE for per-user access, the default." >&2
      exit 1
    fi
  fi
fi

# Which backend image to build and run. See build-push.sh for the full description.
#
#   gpu (default)  Real detection on GPU nodes (CUDA + MinkowskiEngine), every model.
#                  What a dev/prod deployment runs.
#   cpu            Real detection on CPU, slower, no GPU node. FCAF3D excepted.
#   stub           No ML stack; synthetic placeholder detections. For testing the
#                  pipeline and infrastructure. The only variant CI can build.
#
# The backend image tag carries the variant, so deploy.sh must resolve and pass it the
# same way for both the preflight check and the build; it is threaded through
# build-push.sh's --variant.
BUILD_VARIANT="${BUILD_VARIANT:-gpu}"
case "${BUILD_VARIANT}" in
  gpu | cpu | stub) ;;
  *)
    echo "error: BUILD_VARIANT must be gpu, cpu or stub, not '${BUILD_VARIANT}'." >&2
    exit 1
    ;;
esac

# Whether the backend may download the pretrained checkpoints. All of them were
# trained on datasets licensed for non-commercial use only, so the default is no: the
# deployment then detects only with models you train or register (README,
# "Commercially clean model"). ALLOW_NONCOMMERCIAL_WEIGHTS=1 opts in; set it only if
# your use is permitted by those licenses.
case "${ALLOW_NONCOMMERCIAL_WEIGHTS:-0}" in
  1 | true | yes) ALLOW_NONCOMMERCIAL=true ;;
  0 | false | no | "") ALLOW_NONCOMMERCIAL=false ;;
  *)
    echo "error: ALLOW_NONCOMMERCIAL_WEIGHTS must be 1 or 0, not '${ALLOW_NONCOMMERCIAL_WEIGHTS}'." >&2
    exit 1
    ;;
esac

if [ -n "${ALLOW_INSECURE_HTTP:-}" ]; then
  cat >&2 <<'MSG'
error: ALLOW_INSECURE_HTTP is no longer supported.

The ALB signs users in with Cognito, and it can only do that on an HTTPS listener, so
there is no plain-HTTP internet mode any more. For a cluster with no domain or
certificate, deploy without a load balancer and reach the UI through kubectl:

    unset ALLOW_INSECURE_HTTP
    export ACCESS_MODE=port-forward
MSG
  exit 1
fi

case "${ACCESS_MODE}" in
  public)
    if [ -z "${ACM_CERT_ARN:-}" ] || [ -z "${INGRESS_HOST:-}" ]; then
      cat >&2 <<'MSG'
error: ACCESS_MODE=public needs ACM_CERT_ARN and INGRESS_HOST.

The ALB serves the UI and API to the internet and signs every user in with Cognito
first. It can only do that over HTTPS, and Cognito only returns users to a registered
hostname. Set both:

    export ACM_CERT_ARN=arn:aws:acm:<region>:<account>:certificate/<id>
    export INGRESS_HOST=<hostname on that certificate>

Optionally restrict who can reach the ALB at all:

    export INGRESS_INBOUND_CIDRS=203.0.113.0/24,198.51.100.7/32

With no domain, deploy without a load balancer instead:

    export ACCESS_MODE=port-forward
MSG
      exit 1
    fi
    # The Cognito callback URL must be all lowercase, and Terraform and Helm both
    # build theirs from this value.
    INGRESS_HOST="$(printf '%s' "${INGRESS_HOST}" | tr '[:upper:]' '[:lower:]')"

    # ALB authentication with Cognito is available only in some Regions. The ALB lives
    # in the certificate's Region. Warn rather than fail: the list grows over time.
    CERT_REGION="$(printf '%s' "${ACM_CERT_ARN}" | cut -d: -f4)"
    ALB_COGNITO_REGIONS=" us-east-1 us-east-2 us-west-1 us-west-2 ca-central-1 ca-west-1 eu-north-1 eu-south-1 eu-south-2 eu-central-1 eu-central-2 eu-west-1 eu-west-2 eu-west-3 sa-east-1 ap-east-1 ap-northeast-1 ap-northeast-2 ap-northeast-3 ap-south-1 ap-south-2 ap-southeast-1 ap-southeast-2 ap-southeast-3 ap-southeast-4 me-central-1 me-south-1 af-south-1 il-central-1 "
    case "${ALB_COGNITO_REGIONS}" in
      *" ${CERT_REGION} "*) ;;
      *) echo "warning: ${CERT_REGION} is not in the list of Regions where ALB supports Cognito authentication. Check the Elastic Load Balancing documentation before continuing." >&2 ;;
    esac
    ;;
  port-forward)
    ;;
  *)
    echo "error: ACCESS_MODE must be public or port-forward, not '${ACCESS_MODE}'." >&2
    exit 1
    ;;
esac

# The image tag, resolved now so that a dirty tree fails here rather than at step 5,
# after the apply. Step 5 resolves it again for the build.
PREFLIGHT_TAG="$("${SCRIPT_DIR}/build-push.sh" --variant "${BUILD_VARIANT}" --check)"
echo "Image tag: ${PREFLIGHT_TAG#IMAGE_TAG=} (variant: ${BUILD_VARIANT})"

# The state is written with the state bucket's own KMS key (lib/state-backend.sh).
state_backend_args "${TF_STATE_BUCKET}" "pointcloud-detection/${ENV}/terraform.tfstate" \
  "${TF_STATE_REGION:-us-east-1}"
terraform init "${BACKEND_ARGS[@]}"

# Plan, review, then apply exactly the plan that was reviewed (THREAT_MODEL.md T62). A
# saved plan holds every value it plans in clear text, sensitive ones included (in
# public mode, the Cognito app client secret), so it is written to a private temporary
# directory and removed however the script exits.
PLAN_DIR="$(mktemp -d)"
chmod 0700 "${PLAN_DIR}"
trap 'rm -rf "${PLAN_DIR}"' EXIT

# -detailed-exitcode: 0 means no changes, 2 means changes, anything else is an error.
PLAN_RC=0
terraform plan -var-file="environments/${ENV}/terraform.tfvars" \
  -var "access_mode=${ACCESS_MODE}" \
  -var "app_host=${INGRESS_HOST:-}" \
  -detailed-exitcode \
  -out="${PLAN_DIR}/tfplan" || PLAN_RC=$?
case "${PLAN_RC}" in
  0)
    echo "No infrastructure changes; nothing to apply."
    ;;
  2)
    if [ "${AUTO_APPROVE:-}" = "1" ]; then
      echo "AUTO_APPROVE=1: applying the plan above without asking."
    else
      read -r -p "Apply the plan above? Only 'yes' is accepted: " CONFIRM || CONFIRM=""
      if [ "${CONFIRM}" != "yes" ]; then
        echo "Aborted; nothing was applied. AUTO_APPROVE=1 skips this prompt." >&2
        exit 1
      fi
    fi
    terraform apply "${PLAN_DIR}/tfplan"
    ;;
  *)
    exit "${PLAN_RC}"
    ;;
esac
rm -rf "${PLAN_DIR}"

# Capture outputs
CLUSTER_NAME=$(terraform output -raw cluster_name)
REGION=$(terraform output -raw cluster_region)
ECR_BACKEND=$(terraform output -json ecr_repository_urls | jq -r '.["pointcloud-detection-backend"]')
ECR_FRONTEND=$(terraform output -json ecr_repository_urls | jq -r '.["pointcloud-detection-frontend"]')
SQS_URL=$(terraform output -raw sqs_queue_url)
S3_INPUT=$(terraform output -raw s3_input_bucket)
S3_OUTPUT=$(terraform output -raw s3_output_bucket)
S3_TILES=$(terraform output -raw s3_tiles_bucket)
S3_MODELS=$(terraform output -raw s3_models_bucket)
JOBS_TABLE=$(terraform output -raw jobs_table_name)
BACKEND_ROLE=$(terraform output -json iam_role_arns | jq -r '.backend')
WORKER_ROLE=$(terraform output -json iam_role_arns | jq -r '.worker')
VPC_ID=$(terraform output -raw vpc_id)
VPC_CIDR=$(terraform output -raw vpc_cidr)
# Helm list syntax: {a,b,c}. These are where the ALB connects to the api and frontend
# pods from.
ALB_SOURCE_CIDRS="{$(terraform output -json public_subnet_cidrs | jq -r 'join(",")')}"
KEDA_ROLE=$(terraform output -json addon_role_arns | jq -r '.keda_operator')
LBC_ROLE=$(terraform output -json addon_role_arns | jq -r '.alb_controller')
CA_ROLE=$(terraform output -json addon_role_arns | jq -r '.cluster_autoscaler')
KEDA_NAMESPACE=$(terraform output -raw keda_namespace)
FLUENT_BIT_ROLE=$(terraform output -json addon_role_arns | jq -r '.fluent_bit')
APP_CLIENT_GROUP=$(terraform output -raw app_client_group)
# Cognito settings for the ALB's sign-in action, read in public mode only. In
# port-forward mode the output is null, and Terraform records no null output, so
# `terraform output cognito` fails instead of printing null: read unconditionally, it
# stopped every port-forward deploy here, before the first add-on (found on a cluster).
COGNITO=null
BACKEND_IDENTITY_ARGS=()
if [ "${ACCESS_MODE}" = "public" ]; then
  COGNITO=$(terraform output -json cognito)
  # What per-user mode checks each sign-in against (api/identity.py). Checked here, as
  # the role ARNs are below: jq prints "null" for a missing key, which the chart would
  # accept and the API would refuse at startup.
  POOL_ID="$(jq -r '.user_pool_id' <<<"${COGNITO}")"
  APP_CLIENT_ID="$(jq -r '.client_id' <<<"${COGNITO}")"
  if ! [[ "${POOL_ID}" =~ ^[a-z]{2}-[a-z]+-[0-9]+_[A-Za-z0-9]+$ ]] || ! [[ "${APP_CLIENT_ID}" =~ ^[A-Za-z0-9]+$ ]]; then
    echo "error: terraform output cognito has no valid user_pool_id and client_id." >&2
    exit 1
  fi
  BACKEND_IDENTITY_ARGS=(
    --set-string identity.userPoolId="${POOL_ID}"
    --set-string identity.clientId="${APP_CLIENT_ID}"
  )
fi

# jq prints "null" for a missing key, which would go on a service account as its role
# and fail only later, as an add-on with no AWS credentials.
for role in KEDA_ROLE LBC_ROLE CA_ROLE FLUENT_BIT_ROLE; do
  case "${!role}" in
    arn:*) ;;
    *) echo "error: terraform output addon_role_arns has no role ARN for ${role}." >&2; exit 1 ;;
  esac
done

# Step 2: Configure kubectl
echo ""
echo ">>> Step 2: Configuring kubectl..."
# A kubeconfig of this run's own, as destroy.sh uses: `aws eks update-kubeconfig`
# without --kubeconfig rewrites the first file in KUBECONFIG, which for a relay user is
# the relay kubeconfig, and would point it back at the unreachable private endpoint.
# Every kubectl and helm call below uses this one.
KUBE_DIR="$(mktemp -d)"
chmod 0700 "${KUBE_DIR}"
trap 'rm -rf "${KUBE_DIR}"' EXIT
export KUBECONFIG="${KUBE_DIR}/kubeconfig"
write_kubeconfig "${CLUSTER_NAME}" "${REGION}" "${KUBECONFIG}"

# Cluster access is an EKS access entry that Terraform manages, granting cluster-admin
# to the identity that applies the stack. Nothing else has it, the bastion included.
# Checked before anything is installed so that running this as another identity, or
# from a network that cannot reach a private endpoint, stops with the reason.
if ! kubectl auth can-i '*' '*' --all-namespaces --request-timeout=30s >/dev/null 2>&1; then
  cat >&2 <<MSG
error: $(aws sts get-caller-identity --query Arn --output text 2>/dev/null || echo "this identity") cannot administer cluster ${CLUSTER_NAME}, or cannot reach it.

Run this script as an identity with a cluster-admin access entry
(cluster_admin_principal_arns, or the identity that first applied the stack when that
is empty), from a network that can reach the cluster endpoint.
$(cluster_access_hint)
The infrastructure is applied; running this again plans no changes and carries on
from here.
MSG
  exit 1
fi

# Step 3: Create namespace
echo ""
echo ">>> Step 3: Creating namespace..."
kubectl create namespace pointcloud-detection --dry-run=client -o yaml | kubectl apply -f -
# Pod Security Admission (THREAT_MODEL.md T48). The charts meet the restricted profile.
# enforce rejects any pod here that does not, a hand-edited one included; warn and
# audit report violations to the client and in the audit log.
kubectl label namespace pointcloud-detection --overwrite \
  pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/warn=restricted \
  pod-security.kubernetes.io/audit=restricted

# Step 4: Install Helm addons
echo ""
echo ">>> Step 4: Installing cluster addons..."

# `aws-for-fluent-bit` lives in the eks repo, not fluent. The fluent repo was added
# here previously and is not used: it serves the community `fluent-bit` chart, whose
# service account is named differently, so it would not match the IRSA trust policy
# Terraform scopes to system:serviceaccount:kube-system:aws-for-fluent-bit.
helm repo add eks https://aws.github.io/eks-charts || true
helm repo add autoscaler https://kubernetes.github.io/autoscaler || true
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin || true
helm repo add kedacore https://kedacore.github.io/charts || true
helm repo update

# AWS Load Balancer Controller
# Chart versions are pinned throughout. Unpinned, each deploy installs whatever the
# upstream repo publishes that day: for the controller that would be a new major
# version, out of step with the v2.13.4 IAM policy in modules/addons. v2.13 supports
# every annotation the frontend chart uses, including authenticate-cognito.
#
# Only public mode has an Ingress, so port-forward mode does not install it.
#
# The controller gets its credentials from the IRSA role Terraform made for it, whose
# trust policy names this service account. Nodes set the IMDS hop limit to 1, so the
# pod cannot fall back to the node role or read its Region and VPC from instance
# metadata; both are passed. Without the role it cannot create the ALB or its Cognito
# rules, and on destroy it never clears the Ingress finalizers.
#
# --wait, because the chart registers webhooks that every Service and Pod in the
# cluster then passes through, and they fail closed: until the controller's pods are
# ready, no Service can be created anywhere. The next chart below, the Cluster
# Autoscaler's, was refused its Service that way on a sandbox cluster.
if [ "${ACCESS_MODE}" = "public" ]; then
  helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
    -n kube-system \
    --version 1.13.4 \
    --set clusterName="${CLUSTER_NAME}" \
    --set region="${REGION}" \
    --set vpcId="${VPC_ID}" \
    --set serviceAccount.create=true \
    --set serviceAccount.name=aws-load-balancer-controller \
    --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="${LBC_ROLE}" \
    --wait --timeout 5m
fi

# Cluster Autoscaler priority-expander ConfigMap.
# MUST be applied BEFORE Cluster Autoscaler starts. If it is absent the priority
# expander degrades to effectively unranked selection, so you silently get the
# old random node-group choice while believing you have a cost-ordered ladder.
echo "    - applying cluster-autoscaler priority-expander ConfigMap"
(cd "${INFRA_DIR}" && terraform output -raw gpu_cluster_autoscaler_priority_configmap) \
  | kubectl apply -f -

# Cluster Autoscaler
#
#   expander=priority,least-waste
#     Walk the GPU cost ladder defined by gpu_node_pools[*].priority; break ties
#     within a rung on least waste rather than at random.
#
#   balance-similar-node-groups=false
#     LOAD-BEARING. Every GPU pool carries identical labels and the same taint,
#     so CA sees them as similar. With balancing ON it splits one scale-up across
#     pools and can launch an expensive rung alongside a cheap one.
#
#   max-node-provision-time=5m  (default 15m)
#     The priority expander does NOT retry a failed rung. A rung is only excluded
#     once CA's provision timeout expires and the node group enters backoff, so
#     this value IS the per-rung latency cost of walking the ladder.
#
#   scale-down-unneeded-time=5m  (default 10m)
#     GPU nodes are the expensive ones and usage here is bursty (~2h/day), so
#     reclaim them sooner.
#
#   rbac.serviceAccount.*
#     The autoscaler's IRSA role (THREAT_MODEL.md T38). Its trust policy names
#     system:serviceaccount:kube-system:cluster-autoscaler, and the chart would
#     otherwise name the account cluster-autoscaler-aws-cluster-autoscaler, with no
#     annotation. Without the role the autoscaler has no AWS credentials, since the
#     IMDS hop limit of 1 keeps it off the node role, and GPU node groups never
#     scale up.
# 9.51.0 ships Cluster Autoscaler 1.33, matching the default cluster_version.
helm upgrade --install cluster-autoscaler autoscaler/cluster-autoscaler \
  -n kube-system \
  --version 9.51.0 \
  --set autoDiscovery.clusterName="${CLUSTER_NAME}" \
  --set awsRegion="${REGION}" \
  --set 'extraArgs.expander=priority\,least-waste' \
  --set extraArgs.balance-similar-node-groups=false \
  --set extraArgs.max-node-provision-time=5m \
  --set extraArgs.scale-down-unneeded-time=5m \
  --set rbac.serviceAccount.create=true \
  --set rbac.serviceAccount.name=cluster-autoscaler \
  --set rbac.serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="${CA_ROLE}"

# NVIDIA Device Plugin
helm upgrade --install nvidia-device-plugin nvdp/nvidia-device-plugin \
  -n kube-system \
  --version 0.20.1 \
  --set gfd.enabled=true

# Fluent Bit — ships pod logs to CloudWatch Logs.
#
# Terraform has always created the IRSA role for this, but nothing installed the
# chart, so the role sat unused and no pod logs ever left the cluster. Worker logs are
# the only record of a job's failure reason, which made this the difference between a
# diagnosable failure and a silent one.
#
# logGroupName MUST sit under /aws/eks/${CLUSTER_NAME}/. The Terraform policy is scoped
# to arn:aws:logs:...:log-group:/aws/eks/${cluster_name}/*, while the chart defaults to
# /aws/eks/fluentbit-cloudwatch/logs — outside that scope, so an unmodified install
# fails with AccessDenied on CreateLogGroup and drops every log line.
#
# serviceAccount.name is set explicitly rather than left to the chart's fullname
# template. It has to equal the name the trust policy is scoped to
# (system:serviceaccount:kube-system:aws-for-fluent-bit); relying on the release name
# to produce it works but breaks silently if the release is ever renamed.
#
# tolerations[0].operator=Exists, with no key, tolerates every taint, so the DaemonSet
# runs on every node. GPU nodes carry nvidia.com/gpu:NoSchedule and the chart sets no
# tolerations, so without this no worker log ever left a GPU node (THREAT_MODEL.md
# T41). It also covers any tainted pool an adopter adds.
#
# Version pinned for the same reason as KEDA below: this chart carries both a
# deprecated `cloudWatch` output and the current `cloudWatchLogs` one, and an
# unpinned upgrade can move which is the default underneath a working deployment.
helm upgrade --install aws-for-fluent-bit eks/aws-for-fluent-bit \
  -n kube-system \
  --version 0.2.0 \
  --set cloudWatchLogs.enabled=true \
  --set cloudWatchLogs.region="${REGION}" \
  --set cloudWatchLogs.logGroupName="/aws/eks/${CLUSTER_NAME}/workloads" \
  --set cloudWatchLogs.logStreamPrefix="${CLUSTER_NAME}-" \
  --set cloudWatchLogs.autoCreateGroup=true \
  --set cloudWatchLogs.logRetentionDays=30 \
  --set serviceAccount.create=true \
  --set serviceAccount.name=aws-for-fluent-bit \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="${FLUENT_BIT_ROLE}" \
  --set 'tolerations[0].operator=Exists' \
  --wait --timeout 5m

# KEDA — closes the queue-depth -> worker-replicas half of the scaling loop.
# Cluster Autoscaler above only reacts to Pending pods; without KEDA nothing
# creates those pods from SQS backlog.
#
# MUST be installed BEFORE the backend chart: the chart ships a ScaledObject and a
# TriggerAuthentication, and their CRDs do not exist until KEDA is installed. Helm
# fails the release with "no matches for kind ScaledObject" otherwise.
#
# The chart version is pinned deliberately. KEDA CRDs are versioned with the chart,
# so an unpinned upgrade can change the ScaledObject schema underneath a working
# deployment.
#
# podIdentity.aws.irsa annotates only the keda-operator service account, which is
# exactly what the Terraform trust policy is scoped to
# (system:serviceaccount:${KEDA_NAMESPACE}:keda-operator).
#
# permissions.operator.restrict.secret and watchNamespace narrow the operator's RBAC
# (THREAT_MODEL.md T67). By default it can list and watch every Secret in the cluster,
# and its ClusterRole is bound cluster-wide. With them it reads Secrets only in its own
# namespace, and its role is bound only there and in pointcloud-detection, the one
# namespace with a ScaledObject. The TriggerAuthentication uses the operator's IRSA
# role rather than a Secret, so it needs nothing more. watchNamespace needs that
# namespace to exist, which step 3 ensures.
helm upgrade --install keda kedacore/keda \
  -n "${KEDA_NAMESPACE}" --create-namespace \
  --version 2.20.2 \
  --set podIdentity.aws.irsa.enabled=true \
  --set podIdentity.aws.irsa.roleArn="${KEDA_ROLE}" \
  --set permissions.operator.restrict.secret=true \
  --set watchNamespace=pointcloud-detection \
  --wait --timeout 5m

# Upgrading from the token version (docs/EKS_DEPLOY_RUNBOOK.md). Its one Ingress,
# pointcloud-ingress, has no sign-in, because that API checked a token itself. The
# images pushed next have no token check, and any api pod that starts from then on runs
# one, while that ALB would still forward /api to it until the frontend chart replaced
# the Ingress in step 6, a step that can fail. So the old ALB goes first, and nothing is
# served from the internet until the Cognito Ingresses exist. The controller deletes the
# ALB and then the Ingress; a timeout stops the script before anything is pushed.
if kubectl -n pointcloud-detection get ingress pointcloud-ingress >/dev/null 2>&1; then
  echo "    - removing the pre-sign-in Ingress pointcloud-ingress and its ALB"
  kubectl -n pointcloud-detection delete ingress pointcloud-ingress --timeout=5m
fi

# Step 5: Build and push container images
echo ""
echo ">>> Step 5: Building and pushing container images..."
# stdout is the one line IMAGE_TAG=<tag>; the build output goes to stderr.
BUILD_OUTPUT="$("${SCRIPT_DIR}/build-push.sh" --variant "${BUILD_VARIANT}" "${REGION}" "${ECR_BACKEND}" "${ECR_FRONTEND}")"
IMAGE_TAG="${BUILD_OUTPUT#IMAGE_TAG=}"
if [ "${IMAGE_TAG}" = "${BUILD_OUTPUT}" ] || [ -z "${IMAGE_TAG}" ]; then
  echo "error: build-push.sh did not report an image tag." >&2
  exit 1
fi
# build-push.sh reports the backend's tag, <base>-<variant>. The frontend does not
# change between variants, so it pushes that image under <base> alone. Given the
# backend's tag, the frontend pulled a tag that does not exist, in every variant.
FRONTEND_IMAGE_TAG="${IMAGE_TAG%"-${BUILD_VARIANT}"}"
if [ "${FRONTEND_IMAGE_TAG}" = "${IMAGE_TAG}" ]; then
  echo "error: image tag ${IMAGE_TAG} does not end in -${BUILD_VARIANT}." >&2
  exit 1
fi

# Step 6: Deploy workloads with Helm
echo ""
echo ">>> Step 6: Deploying workloads..."

# The API no longer takes a bearer token: in public mode the ALB signs users in, and in
# port-forward mode their IAM identity is the gate. A Secret left by an earlier version
# of this script is unused; it is reported, not deleted.
if kubectl -n pointcloud-detection get secret pointcloud-api-auth >/dev/null 2>&1; then
  echo "[i] secret pointcloud-api-auth is no longer used. Remove it with:"
  echo "      kubectl -n pointcloud-detection delete secret pointcloud-api-auth"
fi

# Where the worker runs. The gpu image needs a GPU node; the cpu and stub images need
# none, so they get no GPU request, node selector or toleration. Without this a
# CPU-only worker waited for a GPU node, which the autoscaler would launch and bill,
# and with no GPU pool it never ran at all (found on a cluster).
case "${BUILD_VARIANT}" in
  gpu) WORKER_GPU=true WORKER_DEVICE=auto ;;
  *) WORKER_GPU=false WORKER_DEVICE=cpu ;;
esac

# The ARN of the ALB the Load Balancer Controller built for the Ingresses, or nothing
# while there is none. Per-user mode pins it: the API accepts only sign-in claims that
# this ALB signed (THREAT_MODEL.md T31). The Ingress names its DNS name once the
# controller has created it.
alb_arn() {
  local host arn
  host="$(kubectl -n pointcloud-detection get ingress pointcloud-spa \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  [ -n "${host}" ] || return 0
  # It goes into the lookup's query, so only a DNS name is accepted.
  case "${host}" in
    *[!A-Za-z0-9.-]*)
      echo "error: the Ingress names an unexpected load balancer hostname." >&2
      return 1
      ;;
  esac
  if ! arn="$(aws elbv2 describe-load-balancers --region "${REGION}" \
    --query "LoadBalancers[?DNSName=='${host}'].LoadBalancerArn | [0]" --output text)"; then
    echo "error: could not look up the ALB ${host}; this needs elasticloadbalancing:DescribeLoadBalancers." >&2
    return 1
  fi
  case "${arn}" in
    arn:*) printf '%s' "${arn}" ;;
  esac
}

ALB_ARN=""
if [ "${ACCESS_MODE}" = "public" ]; then
  # A failed lookup stops the script here, before a working backend is rolled to an
  # empty ARN. No ALB yet, as on a first deploy, keeps what the backend has, if any,
  # until the wait below finds the ALB.
  ALB_ARN="$(alb_arn)"
  if [ -z "${ALB_ARN}" ]; then
    ALB_ARN="$(helm get values pointcloud-backend -n pointcloud-detection -o json 2>/dev/null \
      | jq -r '.identity.albArn // empty' || true)"
  fi
fi

# image.tag is the commit SHA from step 5, so each deploy names exactly the image it
# runs (THREAT_MODEL.md T29). --set-string, because a 12-digit hex SHA can be all
# digits, and --set would then pass it as a number. It is called again once the ALB
# exists, with ALB_ARN set.
install_backend() {
  helm upgrade --install pointcloud-backend "${INFRA_DIR}/helm/backend" \
    -n pointcloud-detection \
    --set image.repository="${ECR_BACKEND}" \
    --set-string image.tag="${IMAGE_TAG}" \
    --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="${BACKEND_ROLE}" \
    --set workerServiceAccount.annotations."eks\.amazonaws\.com/role-arn"="${WORKER_ROLE}" \
    --set aws.region="${REGION}" \
    --set aws.s3.inputBucket="${S3_INPUT}" \
    --set aws.s3.outputBucket="${S3_OUTPUT}" \
    --set aws.s3.tilesBucket="${S3_TILES}" \
    --set aws.s3.modelsBucket="${S3_MODELS}" \
    --set aws.sqs.queueUrl="${SQS_URL}" \
    --set jobStore.backend=dynamodb \
    --set jobStore.tableName="${JOBS_TABLE}" \
    --set models.allowNoncommercialWeights="${ALLOW_NONCOMMERCIAL}" \
    --set accessMode="${ACCESS_MODE}" \
    --set-string publicHost="${INGRESS_HOST:-}" \
    --set networkPolicy.vpcCidr="${VPC_CIDR}" \
    --set "networkPolicy.albSourceCidrs=${ALB_SOURCE_CIDRS}" \
    --set worker.gpu.enabled="${WORKER_GPU}" \
    --set-string worker.device="${WORKER_DEVICE}" \
    --set-string accessScope="${ACCESS_SCOPE}" \
    --set-string identity.albArn="${ALB_ARN}" \
    ${BACKEND_IDENTITY_ARGS[@]+"${BACKEND_IDENTITY_ARGS[@]}"}
}
install_backend

FRONTEND_ARGS=(
  --set accessMode="${ACCESS_MODE}"
  --set-string portForward.group="${APP_CLIENT_GROUP}"
)
if [ "${ACCESS_MODE}" = "public" ]; then
  # Commas separate values in helm --set, so escape the ones inside the CIDR list.
  INBOUND_CIDRS="${INGRESS_INBOUND_CIDRS:-}"
  FRONTEND_ARGS+=(
    --set-string ingress.certificateArn="${ACM_CERT_ARN}"
    --set-string ingress.host="${INGRESS_HOST}"
    --set-string ingress.inboundCidrs="${INBOUND_CIDRS//,/\\,}"
    --set "networkPolicy.albSourceCidrs=${ALB_SOURCE_CIDRS}"
    --set-string ingress.auth.userPoolArn="$(jq -r '.user_pool_arn' <<<"${COGNITO}")"
    --set-string ingress.auth.userPoolClientId="$(jq -r '.client_id' <<<"${COGNITO}")"
    --set-string ingress.auth.userPoolDomain="$(jq -r '.domain_prefix' <<<"${COGNITO}")"
    --set ingress.auth.sessionTimeout="$(jq -r '.session_timeout_seconds' <<<"${COGNITO}")"
  )
fi

# aws.region and aws.s3.inputBucket feed the frontend's Content-Security-Policy
# (THREAT_MODEL.md T35): the browser uploads straight to the input bucket with
# presigned URLs, so connect-src has to name that bucket's endpoint.
helm upgrade --install pointcloud-frontend "${INFRA_DIR}/helm/frontend" \
  -n pointcloud-detection \
  --set image.repository="${ECR_FRONTEND}" \
  --set-string image.tag="${FRONTEND_IMAGE_TAG}" \
  --set aws.region="${REGION}" \
  --set aws.s3.inputBucket="${S3_INPUT}" \
  "${FRONTEND_ARGS[@]}"

# On a first deploy the ALB exists only now, once the controller has acted on the
# Ingresses, so the backend was installed without its ARN and /api answers 503. Wait
# for the ALB, then install the backend again with it. On later deploys the ARN was
# found before the first install, and nothing changes here.
if [ "${ACCESS_MODE}" = "public" ]; then
  echo ">>> Waiting for the ALB, to pin its ARN for sign-in verification..."
  FOUND_ARN=""
  for _ in $(seq 60); do
    FOUND_ARN="$(alb_arn)" || FOUND_ARN=""
    [ -n "${FOUND_ARN}" ] && break
    sleep 10
  done
  if [ -z "${FOUND_ARN}" ]; then
    echo "warning: no ALB for the Ingresses after 10 minutes. /api answers 503 until" >&2
    echo "the backend knows the ALB's ARN; run this script again once it exists." >&2
  elif [ "${FOUND_ARN}" != "${ALB_ARN}" ]; then
    ALB_ARN="${FOUND_ARN}"
    install_backend
    kubectl -n pointcloud-detection rollout status deployment/api --timeout=5m
  fi
fi

# Step 7: Verify
echo ""
echo ">>> Step 7: Verifying deployment..."

# Sign-in lives in the Ingresses, but two Service changes in this namespace get round
# it (THREAT_MODEL.md T71). The controller lets a Service's auth-* annotations override
# the Ingress's on a rule that forwards to that Service, so `kubectl annotate svc api
# alb.ingress.kubernetes.io/auth-type=none` takes sign-in off /api. A LoadBalancer
# Service selecting the api pods puts them behind a load balancer with none at all. The
# charts create neither; stop if anything else has.
EXPOSED=$(kubectl -n pointcloud-detection get svc -o json | jq -r '
  .items[]
  | select(.spec.type == "LoadBalancer"
      or ((.metadata.annotations // {}) | keys | any(startswith("alb.ingress.kubernetes.io/"))))
  | .metadata.name')
if [ -n "${EXPOSED}" ]; then
  echo "error: these Services could bypass the ALB sign-in; remove the load balancer" >&2
  echo "type or the alb.ingress.kubernetes.io/ annotations:" >&2
  while read -r name; do echo "    ${name}" >&2; done <<<"${EXPOSED}"
  exit 1
fi
kubectl -n pointcloud-detection get pods
if [ "${ACCESS_MODE}" = "public" ]; then
  kubectl -n pointcloud-detection get ingress
fi

# READY=True on the ScaledObject means KEDA assumed its IRSA role and reached SQS.
# READY=False here is the signal that queue-depth scaling is not actually working —
# check `kubectl -n keda logs deploy/keda-operator` for AccessDenied.
echo ""
echo ">>> Queue-depth autoscaling status:"
kubectl -n pointcloud-detection get scaledobject worker 2>/dev/null \
  || echo "    (no ScaledObject — keda.enabled=false?)"

# Add-on credentials (THREAT_MODEL.md T38). Each add-on below calls AWS as its own
# IRSA role. Nodes set the IMDS hop limit to 1, so a pod whose role is missing or
# mistrusted gets no credentials at all, and GPU scale-up, the ALB, log shipping or
# queue-depth scaling then stop without an error anywhere else. Wait for each to be
# ready, then search what it has logged for the AWS SDKs' credential errors.
CREDENTIAL_ERRORS='AccessDenied|UnauthorizedOperation|NoCredentialProviders|WebIdentityErr|InvalidIdentityToken|no EC2 IMDS role found|failed to refresh cached credentials|returned no credentials'
ADDON_FAILED=""
# <namespace> <workload> <pod label selector>
check_addon_credentials() {
  local ns="$1" workload="$2" selector="$3" found
  if ! kubectl -n "${ns}" rollout status "${workload}" --timeout=5m; then
    echo "error: ${ns}/${workload} did not become ready." >&2
    ADDON_FAILED=1
    return 0
  fi
  found="$(kubectl -n "${ns}" logs -l "${selector}" --all-containers --prefix \
    --since=30m --tail=500 2>&1 | grep -E "${CREDENTIAL_ERRORS}" | head -n 5 || true)"
  if [ -n "${found}" ]; then
    echo "error: ${ns}/${workload} logged AWS credential errors:" >&2
    printf '%s\n' "${found}" | sed 's/^/    /' >&2
    ADDON_FAILED=1
  else
    echo "    ${ns}/${workload}: ready, no credential errors logged"
  fi
}

echo ""
echo ">>> Add-on AWS credentials:"
check_addon_credentials kube-system deployment/cluster-autoscaler-aws-cluster-autoscaler \
  app.kubernetes.io/instance=cluster-autoscaler
if [ "${ACCESS_MODE}" = "public" ]; then
  check_addon_credentials kube-system deployment/aws-load-balancer-controller \
    app.kubernetes.io/instance=aws-load-balancer-controller
fi
check_addon_credentials kube-system daemonset/aws-for-fluent-bit \
  app.kubernetes.io/instance=aws-for-fluent-bit
check_addon_credentials "${KEDA_NAMESPACE}" deployment/keda-operator app=keda-operator
if [ -n "${ADDON_FAILED}" ]; then
  cat >&2 <<'MSG'

error: an add-on above is not ready or has no working AWS credentials. Check that its
service account carries the eks.amazonaws.com/role-arn annotation for the role in
`terraform output addon_role_arns`, and that the role's trust policy names that
service account.

Do not work around this by raising the nodes' IMDS hop limit. That gives every pod on
the node the node's IAM role (THREAT_MODEL.md T38).
MSG
  exit 1
fi

echo ""
echo "============================================"
echo "Deployment complete!"
echo ""
if [ "${ACCESS_MODE}" = "public" ]; then
  USER_POOL_ID=$(jq -r '.user_pool_id' <<<"${COGNITO}")
  cat <<MSG
1. Point DNS for ${INGRESS_HOST} at the ALB (a CNAME, or an alias record in Route 53):
     kubectl -n pointcloud-detection get ingress pointcloud-spa \\
       -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
   The ALB can take a few minutes to appear. Use https://${INGRESS_HOST}/ itself: the
   ALB's own DNS name has no sign-in and returns 404.

2. Create a user. Cognito emails a temporary password; nothing is stored here.
     aws cognito-idp admin-create-user --region ${REGION} \\
       --user-pool-id ${USER_POOL_ID} \\
       --username user@example.com \\
       --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \\
       --desired-delivery-mediums EMAIL

3. Open https://${INGRESS_HOST}/ and sign in.
MSG
  if [ "${ACCESS_SCOPE}" = shared ]; then
    cat <<'MSG'

ACCESS_SCOPE=shared: every signed-in user can list, open and delete every user's jobs.
Create users only for people who may see all of them (THREAT_MODEL.md T31).
MSG
  else
    cat <<'MSG'

Each signed-in user reaches only the jobs, uploads, results and models they created
(ACCESS_SCOPE=per-user). The API serves only requests signed in through the ALB, so a
port-forward to the api Service gets 401; operators read S3 and DynamoDB with their
own AWS access instead. ACCESS_SCOPE=shared lets every user see every job.
MSG
  fi
else
  # The upload buckets' CORS origin is built from this port (port_forward_local_port).
  PF_PORT=$(terraform output -raw port_forward_local_port)
  cat <<MSG
Open the UI through a tunnel authorised by your IAM identity:
    kubectl -n pointcloud-detection port-forward svc/frontend ${PF_PORT}:80
then browse to http://localhost:${PF_PORT} (exactly that origin: uploads are allowed from it).
MSG
fi
if [ "${ACCESS_MODE}" = "port-forward" ] || [ "${ACCESS_SCOPE}" = shared ]; then
  cat <<'MSG'

Scripts and CLI tools reach the API the same way, with no token:
    kubectl -n pointcloud-detection port-forward svc/api 8000:80
    curl http://localhost:8000/api/jobs
Other IAM principals need an entry in app_client_principal_arns (terraform.tfvars),
and IAM eks:DescribeCluster on this cluster for `aws eks update-kubeconfig`.
MSG
fi
cat <<MSG

This script used a kubeconfig of its own, now removed. To point yours at the cluster:
    aws eks update-kubeconfig --name ${CLUSTER_NAME} --region ${REGION}
or, with the private endpoint alone, run what this prints:
    terraform -chdir=infrastructure output -raw bastion_relay_commands
============================================
MSG
