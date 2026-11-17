# EKS Deployment & Integration Test Runbook

**For**: infrastructure (deploy) + backend (test)
**Prerequisites**: EKS cluster running, ECR images pushed, kubectl configured.
Run every command here from the repository root.

`infrastructure/scripts/deploy.sh` does all of Step 1 for you: it shows the Terraform
plan and asks before applying it (`AUTO_APPROVE=1` skips the question), tags images
with the commit SHA, installs the add-ons with their IRSA roles, and checks their logs
for credential errors afterwards. The commands below are the same steps by hand.

> **Who sees what.** In public mode each signed-in user reaches only the jobs, uploads,
> results, annotations, training jobs and models they created (`ACCESS_SCOPE=per-user`,
> the default; threat model T31). In per-user mode the API answers only requests signed
> in through the ALB, so a port-forward to the api Service gets 401; read S3 and
> DynamoDB with your own AWS access instead. `ACCESS_SCOPE=shared`, and port-forward
> mode always, let every user and every principal with the port-forward access entry
> list, open and delete every job: create users then only for people who may see all of
> it. In public mode `deploy.sh` asks you to confirm `shared` (`AUTO_APPROVE=1` skips
> the question).
>
> Changing the scope later does not move existing jobs. Going from per-user to shared
> shows everyone every job, and each upload's key names its owner's Cognito user id.
> Going from shared to per-user leaves the jobs created in shared mode with no owner:
> no user can see or delete them until they expire (`JOBS_TTL_DAYS`).

---

## Step 1: Infrastructure — Deploy Workloads

### Get Terraform Outputs

```bash
terraform -chdir=infrastructure output
# Note: cluster_name, sqs_queue_url, s3 bucket names, iam role arns
```

### Configure kubectl

```bash
aws eks update-kubeconfig --region "$(terraform -chdir=infrastructure output -raw cluster_region)" \
  --name "$(terraform -chdir=infrastructure output -raw cluster_name)"
kubectl get nodes  # Verify nodes are Ready
```

Your IAM identity needs a cluster-admin access entry. Terraform creates one for each
principal in `cluster_admin_principal_arns`, or, when that list is empty, for the
identity that first applied the stack, which state records and keeps: a later apply by
another identity gives that identity no access and does not remove the first one's. The
cluster creator is no longer an implicit admin. The
endpoint is private only, so reach it through the SSM relay: run the commands
that `terraform -chdir=infrastructure output -raw bastion_relay_commands` prints, then
use the kubeconfig they write. The relay holds no cluster access of its own.

`deploy.sh` and `destroy.sh` write kubeconfigs of their own and never change yours. To
run them through the relay, start it (step 1 of those commands) and set
`PCD_KUBE_RELAY_PORT=8443`:

```bash
PCD_KUBE_RELAY_PORT=8443 infrastructure/scripts/deploy.sh
PCD_KUBE_RELAY_PORT=8443 infrastructure/scripts/destroy.sh
```

A first deploy stops at the cluster-access check, because the relay is created by that
apply; start the relay and run `deploy.sh` again, which plans no changes and carries on.
`SKIP_KUBE_CLEANUP=1` lets `destroy.sh` skip the Kubernetes cleanup when the cluster
cannot be reached at all. In public mode, delete the ALB and security groups the
Ingress left behind first, or the VPC destroy fails on them.

### Create Namespace

```bash
kubectl create namespace pointcloud-detection --dry-run=client -o yaml | kubectl apply -f -
# Enforce the Pod Security "restricted" profile, which every pod the charts render meets
kubectl label namespace pointcloud-detection --overwrite \
  pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/warn=restricted \
  pod-security.kubernetes.io/audit=restricted
```

### Choose the Access Mode

The API has no login of its own. Access is decided by how the deployment is exposed.
Both charts must be told the same mode, and it must be the mode Terraform was applied
with: Terraform creates the Cognito pool (the `cognito` output) only in public mode,
and derives the upload buckets' CORS origin from the mode and host. A stack applied in
one mode and installed in the other fails uploads with CORS errors, or has no Cognito
values to give the chart.

| Mode | How users get in | Needs |
|------|------------------|-------|
| `public` | `https://<INGRESS_HOST>/`. The ALB signs every request in with Cognito before it reaches a pod. | A hostname, an ACM certificate for it, and the Cognito outputs from Terraform (`access_mode = "public"`, the default) |
| `port-forward` | `kubectl port-forward`, authorised by the caller's own IAM identity through an EKS access entry and a namespaced Role that allows port-forwarding and nothing else | No domain or certificate |

Scripts and CLI tools use `kubectl port-forward` in either mode; there are no API keys.

```bash
export ACCESS_MODE=public   # or port-forward
export INGRESS_HOST="<hostname on the certificate, lowercase>"   # public mode only

# Terraform must have been applied with the same values; these must print them:
terraform -chdir=infrastructure output -raw access_mode
terraform -chdir=infrastructure output -raw app_host      # empty in port-forward mode

# If they differ, apply with them first. deploy.sh passes the same flags:
terraform -chdir=infrastructure apply -var-file=environments/dev/terraform.tfvars \
  -var "access_mode=${ACCESS_MODE}" -var "app_host=${INGRESS_HOST:-}"
```

**Public mode also needs the AWS Load Balancer Controller**, chart 1.13.4 (controller
v2.13), the version the IAM policy and the chart's annotations are written for, with
its IRSA role. Nodes limit instance metadata to one network hop, so the controller's
pod cannot read its Region and VPC there, and both are passed:

```bash
helm repo add eks https://aws.github.io/eks-charts
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system --version 1.13.4 \
  --set clusterName="$(terraform -chdir=infrastructure output -raw cluster_name)" \
  --set region="$(terraform -chdir=infrastructure output -raw cluster_region)" \
  --set vpcId="$(terraform -chdir=infrastructure output -raw vpc_id)" \
  --set serviceAccount.create=true \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="$(terraform -chdir=infrastructure output -json addon_role_arns | jq -r .alb_controller)"
```

### Deploy Backend (API + Worker)

The charts require an image tag and refuse `latest`. Use the commit SHA
`build-push.sh` tags with (it prints `IMAGE_TAG=<tag>`), and pass it with
`--set-string`, because an all-digit SHA would otherwise be read as a number:

```bash
export IMAGE_TAG="$(git rev-parse --short=12 HEAD)"

# Replace these with actual Terraform outputs
export ECR_BACKEND="<account-id>.dkr.ecr.<region>.amazonaws.com/pointcloud-detection-backend"
export BACKEND_ROLE_ARN="<from terraform output iam_role_arns.backend>"
export WORKER_ROLE_ARN="<from terraform output iam_role_arns.worker>"
export SQS_QUEUE_URL="<from terraform output sqs_queue_url>"
export S3_INPUT="<from terraform output s3_input_bucket>"
export S3_OUTPUT="<from terraform output s3_output_bucket>"
export S3_TILES="<from terraform output s3_tiles_bucket>"
export S3_MODELS="<from terraform output s3_models_bucket>"
export JOBS_TABLE="<from terraform output jobs_table_name>"
export REGION="<region>"

helm upgrade --install pointcloud-backend infrastructure/helm/backend \
  -n pointcloud-detection \
  --set image.repository="${ECR_BACKEND}" \
  --set-string image.tag="${IMAGE_TAG}" \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="${BACKEND_ROLE_ARN}" \
  --set workerServiceAccount.annotations."eks\.amazonaws\.com/role-arn"="${WORKER_ROLE_ARN}" \
  --set aws.region="${REGION}" \
  --set aws.s3.inputBucket="${S3_INPUT}" \
  --set aws.s3.outputBucket="${S3_OUTPUT}" \
  --set aws.s3.tilesBucket="${S3_TILES}" \
  --set aws.s3.modelsBucket="${S3_MODELS}" \
  --set aws.sqs.queueUrl="${SQS_QUEUE_URL}" \
  --set jobStore.backend=dynamodb \
  --set jobStore.tableName="${JOBS_TABLE}" \
  --set accessMode="${ACCESS_MODE}" \
  --set-string publicHost="${INGRESS_HOST:-}" \
  --set networkPolicy.vpcCidr="$(terraform -chdir=infrastructure output -raw vpc_cidr)" \
  --set "networkPolicy.albSourceCidrs={$(terraform -chdir=infrastructure output -json public_subnet_cidrs | jq -r 'join(",")')}"
```

### Deploy Frontend

The frontend makes no AWS calls, so it has no IRSA role. Every mode binds the
port-forward Role to the group Terraform maps `app_client_principal_arns` to.

```bash
export ECR_FRONTEND="<account-id>.dkr.ecr.<region>.amazonaws.com/pointcloud-detection-frontend"
APP_CLIENT_GROUP=$(terraform -chdir=infrastructure output -raw app_client_group)
```

In both modes the frontend chart needs `aws.region` and `aws.s3.inputBucket`: nginx
sends a Content-Security-Policy whose `connect-src` names the input bucket, because
the browser uploads to it directly. It also sends X-Frame-Options `DENY` and, in public
mode, HSTS for a year, which the ALB's HTTPS listener sets too.

**Public mode.** The chart refuses to render without the certificate, the host and the
Cognito values, so an internet-facing listener without sign-in cannot be deployed by
accident.

```bash
export ACM_CERT_ARN="<ACM certificate for the hostname>"
COGNITO=$(terraform -chdir=infrastructure output -json cognito)

helm upgrade --install pointcloud-frontend infrastructure/helm/frontend \
  -n pointcloud-detection \
  --set image.repository="${ECR_FRONTEND}" \
  --set-string image.tag="${IMAGE_TAG}" \
  --set aws.region="${REGION}" \
  --set aws.s3.inputBucket="${S3_INPUT}" \
  --set accessMode=public \
  --set-string portForward.group="${APP_CLIENT_GROUP}" \
  --set-string ingress.certificateArn="${ACM_CERT_ARN}" \
  --set-string ingress.host="${INGRESS_HOST}" \
  --set "networkPolicy.albSourceCidrs={$(terraform -chdir=infrastructure output -json public_subnet_cidrs | jq -r 'join(",")')}" \
  --set-string ingress.auth.userPoolArn="$(jq -r .user_pool_arn <<<"${COGNITO}")" \
  --set-string ingress.auth.userPoolClientId="$(jq -r .client_id <<<"${COGNITO}")" \
  --set-string ingress.auth.userPoolDomain="$(jq -r .domain_prefix <<<"${COGNITO}")" \
  --set ingress.auth.sessionTimeout="$(jq -r .session_timeout_seconds <<<"${COGNITO}")"
```

Sessions last 12 hours (Terraform `auth_session_hours`), long enough for a multi-hour
upload; the Cognito refresh token is set to the same length. When a session ends the
UI reloads once and the ALB shows the sign-in page.

**Port-forward mode.** No Ingress and no ALB; nginx in the frontend pod proxies `/api`
to the api Service.

```bash
helm upgrade --install pointcloud-frontend infrastructure/helm/frontend \
  -n pointcloud-detection \
  --set image.repository="${ECR_FRONTEND}" \
  --set-string image.tag="${IMAGE_TAG}" \
  --set aws.region="${REGION}" \
  --set aws.s3.inputBucket="${S3_INPUT}" \
  --set accessMode=port-forward \
  --set-string portForward.group="${APP_CLIENT_GROUP}"
```

### Create a User (Public Mode)

Self sign-up is off, so an operator creates each user. Cognito emails a temporary
password and asks for a new one at first sign-in; nothing is stored on your side.

```bash
USER_POOL_ID=$(terraform -chdir=infrastructure output -json cognito | jq -r .user_pool_id)
aws cognito-idp admin-create-user --region "${REGION}" \
  --user-pool-id "${USER_POOL_ID}" \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
```

- `--desired-delivery-mediums` defaults to SMS, which the pool does not use, so pass
  `EMAIL`.
- The temporary password expires after 7 days; resend it with
  `--message-action RESEND`.
- Do not pass `--temporary-password` or use `admin-set-user-password`: both put a
  password in your shell history.
- Cognito's default email sender allows about 50 messages a day per account. Beyond
  that, configure Amazon SES for the pool.
- To remove access, run `aws cognito-idp admin-disable-user`, which also stops the
  user signing in again, or `admin-user-global-sign-out`, which revokes the user's
  tokens but not their password. The ALB checks Cognito again when the access token
  expires, so access ends within 15 minutes. The UI's **Sign out** is no substitute:
  it ends the session in that browser only and revokes nothing, so a copy of the
  session cookie keeps working until the session ends (Terraform
  `auth_session_hours`). `admin-delete-user` erases the user and everything the pool
  holds for them: the email address, the password verifier and any TOTP secret.
  `destroy.sh` deletes the whole pool.
- MFA is `ON` by default (Terraform `cognito_mfa_configuration`): the hosted sign-in
  page enrols every user in TOTP at first sign-in. `OPTIONAL` is available, but the page
  never prompts anyone to register an authenticator then, so it is password-only.
- With `alarm_email` set, the alarm `<prefix>-cognito-failed-sign-ins` notifies you of
  at least 10 failed sign-ins in 5 minutes (`cognito_failed_sign_in_alarm_threshold`).
- **Review the user list periodically.** Nothing removes people who have left. On a
  schedule, list the users and disable, then delete, anyone who should no longer have
  access:

  ```bash
  aws cognito-idp list-users --region "${REGION}" --user-pool-id "${USER_POOL_ID}" \
    --query 'Users[].{user:Username,status:UserStatus,enabled:Enabled,created:UserCreateDate}'
  aws cognito-idp admin-disable-user --region "${REGION}" --user-pool-id "${USER_POOL_ID}" --username <user>
  aws cognito-idp admin-delete-user  --region "${REGION}" --user-pool-id "${USER_POOL_ID}" --username <user>
  ```

  The pool holds each user's email address (also the username), password verifier,
  TOTP secret and sign-in history until the user or the pool is deleted. See the
  README's *Personal data and retention* section for every other store.

The ALB passes the signed-in user's claims to the pods in `x-amzn-oidc-*` headers.
Nothing in the sample reads them. Anything built on it must verify the signature of
`x-amzn-oidc-data`, and that its `signer` is this ALB, before authorizing on them: any
pod the NetworkPolicy admits can reach the API directly and set those headers itself.

### Upgrading an Existing Deployment

This version changes six things in ways an existing stack must prepare
for. Do these once, before the first `deploy.sh` (or `terraform apply`) of the new
version. A new deployment needs none of them.

**1. The cluster creator's access entry.** New clusters no longer make their creator a
permanent admin; admins are explicit access entries for `cluster_admin_principal_arns`
(or, when that is empty, the identity that first applies this version, which state then keeps). Changing the creator flag
would replace the cluster, so Terraform ignores it on existing clusters, which already
hold the creator's entry. Applying would then fail with `ResourceInUseException` when
it creates the same entry. Find the ARN, then either import the entry or delete it:

Run these from a fresh shell with the same settings `deploy.sh` uses, or Terraform
has no backend to import into and evaluates the configuration with default variables
instead of yours. `ENV`, `ACCESS_MODE` and `INGRESS_HOST` are the values you deploy
with (`INGRESS_HOST` empty in port-forward mode):

```bash
ENV=dev
TF_VARS=(-var-file="environments/${ENV}/terraform.tfvars"
  -var "access_mode=${ACCESS_MODE:-public}" -var "app_host=${INGRESS_HOST:-}")
. infrastructure/scripts/lib/state-backend.sh   # the same backend arguments deploy.sh uses
state_backend_args "${TF_STATE_BUCKET}" "pointcloud-detection/${ENV}/terraform.tfstate" \
  "${TF_STATE_REGION:-us-east-1}" infrastructure
terraform -chdir=infrastructure init "${BACKEND_ARGS[@]}"

CLUSTER=$(terraform -chdir=infrastructure output -raw cluster_name)
aws eks list-access-entries --cluster-name "$CLUSTER"
# The creator's principal ARN from that list. Import it under the ARN it will be
# managed as: one in cluster_admin_principal_arns, or, with that empty, the role you
# are running this as, which the apply records as the default admin.
ARN="<the creator's principal ARN from that list>"

# (a) Import it and its policy association into the new resources
terraform -chdir=infrastructure import "${TF_VARS[@]}" \
  "module.eks.aws_eks_access_entry.cluster_admin[\"$ARN\"]" "$CLUSTER:$ARN"
terraform -chdir=infrastructure import "${TF_VARS[@]}" \
  "module.eks.aws_eks_access_policy_association.cluster_admin[\"$ARN\"]" \
  "$CLUSTER#$ARN#arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

# (b) Or delete it and let the apply create it again (you lose access in between)
aws eks delete-access-entry --cluster-name "$CLUSTER" --principal-arn "$ARN"
```

Where several people or pipelines administer the cluster, set
`cluster_admin_principal_arns` explicitly. With it empty, the first identity to apply
this version becomes the admin and stays so; later callers get no access of their
own. The old bastion's access entry and policy association are destroyed by the
apply.

**2. ECR encryption.** The repositories move to a customer managed KMS key, which
forces Terraform to replace them, and the old repositories' state has no
`force_delete`, so replacing a non-empty one fails. Delete them once first; the apply
recreates them encrypted, `deploy.sh` pushes the backend and frontend images again, and
a training image must be pushed again by hand:

```bash
for r in pointcloud-detection-backend pointcloud-detection-frontend pointcloud-detection-training; do
  aws ecr delete-repository --force --repository-name "$r"
done
```

Running pods keep their images until they restart. Deploy straight after.

**3. The bastion becomes an SSM relay.** The next apply replaces the instance (its user
data changed on purpose), which removes its kubectl and helm binaries and its
cluster-admin access. Operators then use the relay from their own machines
(`terraform -chdir=infrastructure output -raw bastion_relay_commands`), under their own
access entries.

**4. Account-level S3 Block Public Access.** It is no longer managed unless
`manage_account_public_access_block = true`. A `removed` block makes Terraform forget
the old resource without deleting the setting, so nothing changes in the account.
Check it afterwards, and keep it on:

```bash
aws s3control get-public-access-block --account-id "$(aws sts get-caller-identity --query Account --output text)"
```

If you opt in, the resource has `prevent_destroy`; `destroy.sh` takes it out of state
before it plans a destroy (`terraform state rm 'module.s3.aws_s3_account_public_access_block.account_opt_in[0]'`),
so tearing the stack down never deletes it. Turning the variable back off needs that
`state rm` by hand first.

**5. AWS provider 6 and S3 bucket names.** The stack now needs AWS provider 6.37 or
later (the lock file pins 6.71.0), which `terraform init` installs from the committed
lock file. Every bucket is now created in the account's regional namespace, with a name
ending in `-<account>-<region>-an` that S3 lets no other account create, before or after
the stack deletes it. Names in the shared global namespace could be registered first by
anyone who guessed them. The access-logs bucket is now named `<prefix>-logs-…`, so the
longest default name fits S3's 63 characters in every Region. The namespace is not
offered in Middle East (Bahrain) or Middle East (UAE), and the plan refuses those two
Regions.

A bucket's name and namespace are part of its identity, so the next plan replaces all
five buckets: input, output, tiles, models and access logs. The buckets are versioned,
and S3 refuses to delete a bucket that still holds any object version, so the apply
stops at the first one. Copy what you want to keep, such as fine-tuned checkpoints from
the models bucket, then empty each old bucket, including every version. **Empty** in
the S3 console does both. Then apply. `deploy.sh` reads the new names from Terraform's
outputs. The API keeps its model list in memory, so fine-tuned models drop out of it
when the pods restart, as on any restart; their checkpoints stay in the restored bucket.

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
# 1. Before applying: take the old name from state, check this account owns it, back up.
OLD=$(terraform -chdir=infrastructure output -raw s3_models_bucket)
aws s3api head-bucket --bucket "${OLD}" --expected-bucket-owner "${ACCOUNT}"
aws s3 sync "s3://${OLD}" ./models-backup
# 2. Empty the five old buckets, every version included. 3. Apply. 4. Restore:
NEW=$(terraform -chdir=infrastructure output -raw s3_models_bucket)
aws s3api head-bucket --bucket "${NEW}" --expected-bucket-owner "${ACCOUNT}"
aws s3 sync ./models-backup "s3://${NEW}"
```

`head-bucket` with `--expected-bucket-owner` fails unless this account owns the bucket,
so each copy stops before it touches a bucket of the same name in another account.

Provider 6 also stores the bastion's `user_data` in clear text rather than as a hash.
It holds no secret (`infrastructure/modules/bastion/main.tf`). If the plan shows the
bastion replaced, that is safe: it is a stateless SSM relay.

The state bucket can be updated in place: re-run
`infrastructure/scripts/bootstrap-state.sh`, which sets SSE-KMS and refuses a bucket
whose policy it did not write. `deploy.sh` and `destroy.sh` now pass the bucket's
default KMS key to Terraform as `kms_key_id` (`infrastructure/scripts/lib/state-backend.sh`).
Before, `encrypt = true` alone made Terraform write the state with SSE-S3, overriding
the bucket's default. The first run re-initialises the backend in place
(`-reconfigure`; the state does not move), and the next apply rewrites the state
under the key.

**6. The public cluster endpoint is gone.** The `enable_public_endpoint` and
`public_access_cidrs` variables are removed, and the API server has a private endpoint
only. Delete both from your tfvars (Terraform warns that they are undeclared, and
ignores them) and leave `enable_bastion` at its default, `true`. If the cluster had the
public endpoint on, the apply turns it off in place, which drops any kubeconfig that
used it: start the relay from `bastion_relay_commands` and set
`PCD_KUBE_RELAY_PORT=8443` for `deploy.sh`. As on a first deploy, that apply creates the
relay if it did not exist, so `deploy.sh` stops at its cluster-access check; run it
again with the relay up.

### Upgrading from the Token Version

Earlier versions used one Ingress and a shared API token. Upgrading changes both:

- **Remove the old Ingress before anything else.** It has no sign-in, because the old
  API checked the token itself, and the new API does not. If the new backend image
  runs while that ALB still forwards `/api`, the API is open to the internet until the
  frontend chart replaces the Ingress. `deploy.sh` deletes `pointcloud-ingress`, and
  waits for its ALB to go, before it pushes any image. By hand, do the same first:

  ```bash
  kubectl -n pointcloud-detection delete ingress pointcloud-ingress --timeout=5m
  ```

  Then install the backend and frontend charts. The UI is unreachable from the
  internet in between.
- The two new Ingresses form an explicit IngressGroup, so the Load Balancer Controller
  creates a **new ALB with a new DNS name**. Point the DNS record for `INGRESS_HOST` at
  it after the upgrade.
- The `pointcloud-api-auth` Secret is no longer read. Delete it with
  `kubectl -n pointcloud-detection delete secret pointcloud-api-auth`, and clear any
  scripts that send `Authorization: Bearer`.
- `ALLOW_INSECURE_HTTP` and `ingress.allowInsecureHttp` are gone. A cluster without a
  certificate uses port-forward mode.

### Verify Deployment

```bash
# All pods running
kubectl -n pointcloud-detection get pods

# Expected (the worker pod runs the worker and parser containers):
#   api-xxxxx          1/1   Running
#   worker-xxxxx       2/2   Running
#   frontend-xxxxx     1/1   Running

# Public mode: the ALB's DNS name. Point INGRESS_HOST at it (CNAME or Route 53 alias).
# Browse to https://${INGRESS_HOST}/, not to this name: every rule matches the host,
# so the ALB's own name gets a 404 and has no sign-in.
kubectl -n pointcloud-detection get ingress pointcloud-spa
```

---

## Step 2: Backend — Integration Test IT-5 (End-to-End)

### 2.1 Open a Tunnel to the API

The API is not reachable from the internet without a browser sign-in, so tests and
scripts go through a port-forward. It is authorised by your IAM identity; there is no
token to pass. This works in port-forward mode and in public mode with
`ACCESS_SCOPE=shared`. With per-user access, the default in public mode, the API
serves only requests signed in through the ALB: through the tunnel `/health` answers,
and every `/api` route answers 401. Run these steps from the UI there instead. A principal without a cluster-admin access entry needs an
entry in `app_client_principal_arns`, and IAM `eks:DescribeCluster` on the cluster for
`aws eks update-kubeconfig`.

```bash
kubectl -n pointcloud-detection port-forward svc/api 8000:80 &
API=http://localhost:8000
```

Keep the tunnel open only while you need it. Any local process that can reach
`localhost:8000` can use it with your access. Web pages you visit cannot read it: the
API answers only requests addressed to `localhost`, `127.0.0.1` or its own cluster
names, so a page that re-points its own name at your machine (DNS rebinding) gets 421.

### 2.2 Health Check

```bash
curl -s "${API}/health"
# Expected: {"status":"ok"}
```

### 2.3 Upload a Test File

```bash
# Upload a small KITTI .bin file to S3 input bucket
aws s3 cp data/kitti/training/velodyne/000000.bin "s3://${S3_INPUT}/uploads/test-001/000000.bin"

# Or use the API multipart upload:
curl -s -X POST "${API}/api/upload/initiate" \
  -H "Content-Type: application/json" \
  -d '{"filename": "000000.bin", "file_size_bytes": 1920000}'
# Note the upload_id and s3_key from response
```

### 2.4 Create a Detection Job

```bash
curl -s -X POST "${API}/api/jobs" \
  -H "Content-Type: application/json" \
  -d "{
    \"s3_key\": \"uploads/test-001/000000.bin\",
    \"filename\": \"000000.bin\",
    \"config\": {
      \"model_mode\": \"kitti\",
      \"use_intensity\": true,
      \"confidence_threshold\": 0.3
    }
  }" | python -m json.tool
# Note the job_id
```

### 2.5 Monitor Job Status

```bash
JOB_ID="<from above>"

# Poll until complete (check every 10 seconds)
while true; do
  STATUS=$(curl -s "${API}/api/jobs/${JOB_ID}" | python -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "Status: ${STATUS}"
  [ "${STATUS}" = "complete" ] && break
  [ "${STATUS}" = "failed" ] && echo "FAILED!" && break
  sleep 10
done
```

### 2.6 Verify Results

```bash
# Get detection results
curl -s "${API}/api/results/${JOB_ID}/detections" | python -m json.tool | head -30

# Get tile index
curl -s "${API}/api/results/${JOB_ID}/tiles/index" | python -m json.tool

# Check worker logs
kubectl -n pointcloud-detection logs -l app=worker --tail=50
```

### 2.7 Test Frontend

Open the UI in a browser. Public mode: `https://${INGRESS_HOST}/`, which sends you to
the Cognito sign-in page first. Port-forward mode: run
`kubectl -n pointcloud-detection port-forward svc/frontend 8080:80` and open exactly
`http://localhost:8080`, the origin the upload buckets accept. If Terraform's
`port_forward_local_port` is not 8080, use that port instead
(`terraform -chdir=infrastructure output -raw port_forward_local_port`).
- [ ] Upload page loads
- [ ] Can select a file and see upload progress
- [ ] Dashboard shows the job we created
- [ ] Job shows "complete" status
- [ ] "View Results" button works
- [ ] 3D viewer renders point cloud (if tiles generated)

---

## Step 3: Backend — Integration Test IT-6 (Auto-Scaling)

### 3.1 Submit 10 Jobs Rapidly

```bash
for i in $(seq 1 10); do
  curl -s -X POST "${API}/api/jobs" \
    -H "Content-Type: application/json" \
    -d "{
      \"s3_key\": \"uploads/test-001/000000.bin\",
      \"filename\": \"test_${i}.bin\",
      \"config\": {\"model_mode\": \"kitti\", \"use_intensity\": true}
    }" > /dev/null
  echo "Submitted job ${i}"
done
```

### 3.2 Watch Pods Scale

```bash
# Watch worker pod count (should scale up from 1 to multiple)
kubectl -n pointcloud-detection get pods -w -l app=worker

# Check HPA status
kubectl -n pointcloud-detection get hpa
```

### 3.3 Verify All Complete

```bash
# List all jobs, check all complete
curl -s "${API}/api/jobs" | python -c "
import sys, json
data = json.load(sys.stdin)
for job in data['jobs']:
    print(f\"  {job['filename']:20s} status={job['status']}\")
print(f\"Total: {data['total']} jobs\")
"
```

---

## Troubleshooting

| Issue | Check |
|-------|-------|
| Pods not starting | `kubectl -n pointcloud-detection describe pod <name>` |
| ImagePullBackOff | ECR permissions, image exists? `aws ecr describe-images --repository-name pointcloud-detection-backend` |
| Worker not processing | `kubectl -n pointcloud-detection logs -l app=worker` — check SQS URL and IRSA role |
| API returns 500 | `kubectl -n pointcloud-detection logs -l app=api` |
| ALB not provisioning | Check ALB controller: `kubectl -n kube-system logs -l app.kubernetes.io/name=aws-load-balancer-controller` |
| Browser loops back to sign-in, or the ALB returns 401 after sign-in | The Cognito callback must be `https://<INGRESS_HOST>/oauth2/idpresponse` in lowercase; browse to `INGRESS_HOST`, not the ALB's DNS name. Sign-in must finish within 15 minutes. |
| ALB returns 500 on sign-in | The ALB cannot reach Cognito's token endpoint. Check any custom security group or network ACL allows ALB egress on 443. |
| UI calls return 403 `X-PCD-Client header required` | Something other than the SPA is calling `/api` through the ALB. Scripts use `kubectl port-forward svc/api` instead, in port-forward mode or with `ACCESS_SCOPE=shared`. |
| `/api` answers 503 `Sign-in verification is not configured yet` | Per-user mode, and the backend does not know the ALB's ARN yet. `deploy.sh` sets it once the Load Balancer Controller has created the ALB; run it again if it warned that it gave up waiting. |
| `/api` answers 401 `Sign-in required` after a successful sign-in | The API refused the ALB's claims. Its log says why: `kubectl -n pointcloud-detection logs deploy/api -c api \| grep 'did not verify'`. A wrong signer means the ALB was replaced: run `deploy.sh` again. A wrong app client or issuer means the backend's `identity.*` values do not match `terraform output cognito`. |
| The host times out in public mode, with no answer at all | The ALB admits only `INGRESS_INBOUND_CIDRS`, matched against the address your traffic leaves from. That can differ from what an IP-echo site shows, and behind a corporate VPN it can rotate across a NAT pool. The VPC flow logs show what the ALB refused: `REJECT` records with destination port 443 on the ALB's network interfaces. Add that address, or its pool, and run `deploy.sh` again. |
| `/api` answers 503 `Sign-in could not be verified just now` | The api pods could not fetch the ALB's public key from `public-keys.auth.elb.<region>.amazonaws.com`. Check HTTPS egress from the api pods and the VPC's NAT gateway. |
| Uploads fail with a CORS error | The page origin is not in the bucket CORS rule. Public mode: use `https://<INGRESS_HOST>`. Port-forward mode: use `http://localhost:<port_forward_local_port>`, 8080 by default, as both the tunnel's local port and the browser address. |
| `aws eks update-kubeconfig` fails with `AccessDeniedException` on `DescribeCluster` | The access entry covers Kubernetes calls only. Give the principal an IAM policy allowing `eks:DescribeCluster` on the cluster ARN; this stack does not grant it. |
| `kubectl port-forward` is forbidden | The IAM principal needs an entry in `app_client_principal_arns` (Terraform), or cluster admin. |
| S3 access denied | IRSA role not assumed — check SA annotation matches role ARN |
| An add-on logs `AccessDenied`, `NoCredentialProviders` or `WebIdentityErr`, and `deploy.sh` stops | Its service account is missing the IRSA annotation, or its name does not match the role's trust. Fix that. **Never raise the nodes' IMDS hop limit** to get past it: that gives every pod on the node the node's IAM role |
| Worker pod `1/2`, parser container restarting | The parser refuses to start if it can see AWS credentials or a service-account token. Check `kubectl -n pointcloud-detection logs deploy/worker -c parser` and that the pod keeps `eks.amazonaws.com/skip-containers: parser` |
| Pods rejected by Pod Security admission | A hand-edited pod spec that does not meet `restricted`. The charts' own pods do; restore the chart's security context |
| `helm` refuses: `image.tag` is required, or `latest` is refused | Pass the commit SHA with `--set-string image.tag=` |
| `helm` refuses: `networkPolicy.enabled=false renders no NetworkPolicy` | Intended. In shared mode the API has no authentication of its own, so the policies are its only network boundary; in per-user mode they are its first. Set `networkPolicy.acknowledgeUnprotectedApi=true` only on purpose |
| Queue not draining | Worker can't connect to SQS — check endpoint URL and region |

---

## Pass Criteria

- [ ] `curl /health` returns 200
- [ ] Job completes (status → "complete") within 5 minutes
- [ ] Detection JSON has valid results (detections array)
- [ ] Tile index exists (point_format field present)
- [ ] Frontend loads in browser
- [ ] Worker scales up when multiple jobs submitted
- [ ] No error logs in worker/api pods
