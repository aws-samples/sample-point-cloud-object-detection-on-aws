# Step 3: Deploy Workloads to EKS

## Overview
Deploy backend (API + Worker) and frontend using Helm charts.

## Steps

### 3.1 Install Cluster Addons
<!-- TODO: Full helm commands for ALB controller, cluster autoscaler, nvidia plugin -->

### 3.2 Create Namespace
```bash
kubectl create namespace pointcloud-detection
kubectl label namespace pointcloud-detection pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/warn=restricted pod-security.kubernetes.io/audit=restricted
```

### 3.3 How You Will Reach the App
The workshop deploys in **port-forward mode**: there is no load balancer, and your IAM
identity is what lets you in, through `kubectl port-forward`. The API has no password
or token of its own.

If you have a domain and an ACM certificate, you can deploy in **public mode** instead:
an internet-facing ALB that signs every user in with Amazon Cognito. Run Terraform with
`-var access_mode=public -var app_host=<hostname>` (step 1.3) and follow
`docs/EKS_DEPLOY_RUNBOOK.md`, which also covers creating users with
`aws cognito-idp admin-create-user`.

### 3.4 Deploy Backend
The API refuses to start without all four bucket names, and the api and worker pods
get their AWS credentials only from the role annotations on their service accounts,
so every value below is needed. They all come from Terraform:
```bash
tf() { terraform -chdir=infrastructure output "$@"; }
helm upgrade --install pointcloud-backend infrastructure/helm/backend \
  -n pointcloud-detection \
  --set image.repository="$(tf -json ecr_repository_urls | jq -r '.["pointcloud-detection-backend"]')" \
  --set-string image.tag="$TAG" \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="$(tf -json iam_role_arns | jq -r .backend)" \
  --set workerServiceAccount.annotations."eks\.amazonaws\.com/role-arn"="$(tf -json iam_role_arns | jq -r .worker)" \
  --set aws.region="$(tf -raw cluster_region)" \
  --set aws.s3.inputBucket="$(tf -raw s3_input_bucket)" \
  --set aws.s3.outputBucket="$(tf -raw s3_output_bucket)" \
  --set aws.s3.tilesBucket="$(tf -raw s3_tiles_bucket)" \
  --set aws.s3.modelsBucket="$(tf -raw s3_models_bucket)" \
  --set aws.sqs.queueUrl="$(tf -raw sqs_queue_url)" \
  --set jobStore.backend=dynamodb \
  --set jobStore.tableName="$(tf -raw jobs_table_name)" \
  --set accessMode=port-forward \
  --set networkPolicy.vpcCidr="$(tf -raw vpc_cidr)" \
  --set "networkPolicy.albSourceCidrs={$(tf -json public_subnet_cidrs | jq -r 'join(",")')}"
```
`ACCESS_MODE=port-forward infrastructure/scripts/deploy.sh` runs this step, with the
add-ons and the frontend, for you.

The worker pod has two containers: `worker`, which holds the job's AWS credentials, and
`parser`, which reads your uploaded file with no credentials at all.

### 3.5 Deploy Frontend
In port-forward mode the chart renders no Ingress. nginx in the frontend pod serves
the UI and forwards `/api` to the API.
```bash
helm upgrade --install pointcloud-frontend infrastructure/helm/frontend \
  -n pointcloud-detection \
  --set image.repository=<ecr-frontend-url> \
  --set-string image.tag=$TAG \
  --set aws.region=<region> \
  --set aws.s3.inputBucket=<input-bucket> \
  --set accessMode=port-forward \
  --set-string portForward.group="$(terraform -chdir=infrastructure output -raw app_client_group)"
```

### 3.6 Open the App
Leave this running in its own terminal for the rest of the workshop:
```bash
kubectl -n pointcloud-detection port-forward svc/frontend 8080:80
```
Then open exactly `http://localhost:8080`. Uploads go straight from the browser to S3,
and the buckets accept them only from that origin, so `127.0.0.1` or another port will
not work. (8080 is the default of the Terraform variable `port_forward_local_port`. If
you changed it, use that port here instead.)

To call the API from a terminal, open a second tunnel. No token is needed:
```bash
kubectl -n pointcloud-detection port-forward svc/api 8000:80 &
curl http://localhost:8000/health
```

## Validation Checkpoint
- [ ] All pods in Running state (`kubectl get pods -n pointcloud-detection`); the worker shows `2/2`
- [ ] `http://localhost:8080` shows the app while the port-forward runs
- [ ] `curl http://localhost:8000/health` returns `{"status": "ok"}`
