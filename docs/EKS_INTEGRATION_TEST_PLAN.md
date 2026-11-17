# EKS Integration: Configuration & Test Plan

**Date**: Aug 10, 2026
**Cluster**: `pointcloud-detection-dev` (us-east-1, account 111122223333)
**Status**: infrastructure is ready. Worker service-account fix pending.

---

## Pre-Deployment Checklist

### 1. Get Cluster Access

```bash
# Get your IP, then add it to the allowed CIDR list in Terraform. Adding it
# directly to the security group works but the next apply will revert it.
curl -s https://checkip.amazonaws.com

# Get your IAM role ARN
aws sts get-caller-identity --query Arn --output text
# Example: arn:aws:iam::111122223333:role/Admin

# Create EKS access entry
aws eks create-access-entry \
    --cluster-name pointcloud-detection-dev \
    --region us-east-1 \
    --principal-arn <YOUR_ROLE_ARN>

aws eks associate-access-policy \
    --cluster-name pointcloud-detection-dev \
    --region us-east-1 \
    --principal-arn <YOUR_ROLE_ARN> \
    --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
    --access-scope type=cluster

# Configure kubectl
aws eks update-kubeconfig --name pointcloud-detection-dev --region us-east-1

# Verify
kubectl get nodes
```

### 2. Build & Push Images

ECR tags are **immutable** — use git SHA, never `latest`.

```bash
# Get git SHA for tag
TAG=$(git rev-parse --short HEAD)
echo "Using tag: $TAG"

# Login to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 111122223333.dkr.ecr.us-east-1.amazonaws.com

# Build + push backend
docker build -t pointcloud-backend .
docker tag pointcloud-backend 111122223333.dkr.ecr.us-east-1.amazonaws.com/pointcloud-detection-backend:$TAG
docker push 111122223333.dkr.ecr.us-east-1.amazonaws.com/pointcloud-detection-backend:$TAG

# Build + push frontend
docker build -t pointcloud-frontend -f frontend/Dockerfile frontend/
docker tag pointcloud-frontend 111122223333.dkr.ecr.us-east-1.amazonaws.com/pointcloud-detection-frontend:$TAG
docker push 111122223333.dkr.ecr.us-east-1.amazonaws.com/pointcloud-detection-frontend:$TAG
```

### 3. Deploy API + Frontend (Worker Held)

Deploy in this order: backend (API only, worker=0) → frontend → verify → worker.

```bash
TAG=$(git rev-parse --short HEAD)

# Deploy backend (API only — worker blocked until the service-account fix lands)
helm upgrade --install backend infrastructure/helm/backend \
    -n pointcloud-detection --create-namespace \
    --set image.repository=111122223333.dkr.ecr.us-east-1.amazonaws.com/pointcloud-detection-backend \
    --set image.tag=$TAG \
    --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=arn:aws:iam::111122223333:role/pointcloud-detection-dev-backend-role" \
    --set aws.region=us-east-1 \
    --set aws.s3.inputBucket=pointcloud-detection-dev-input-111122223333-us-east-1-an \
    --set aws.s3.outputBucket=pointcloud-detection-dev-output-111122223333-us-east-1-an \
    --set aws.s3.tilesBucket=pointcloud-detection-dev-tiles-111122223333-us-east-1-an \
    --set aws.s3.modelsBucket=pointcloud-detection-dev-models-111122223333-us-east-1-an \
    --set aws.sqs.queueUrl=https://sqs.us-east-1.amazonaws.com/111122223333/pointcloud-detection-dev-jobs \
    --set jobStore.tableName=pointcloud-detection-dev-jobs \
    --set accessMode=port-forward \
    --set worker.replicas=0

# Deploy frontend. Port-forward mode needs no certificate; for public mode with
# Cognito sign-in see the runbook.
helm upgrade --install frontend infrastructure/helm/frontend \
    -n pointcloud-detection \
    --set image.repository=111122223333.dkr.ecr.us-east-1.amazonaws.com/pointcloud-detection-frontend \
    --set image.tag=$TAG \
    --set accessMode=port-forward
```

### 4. Verify Deployment

```bash
# Check pods are running
kubectl -n pointcloud-detection get pods

# Check API is healthy
API_POD=$(kubectl -n pointcloud-detection get pods -l app=backend-api -o jsonpath='{.items[0].metadata.name}')
kubectl -n pointcloud-detection port-forward $API_POD 8000:8000 &
curl http://127.0.0.1:8000/health

# Open the UI (port-forward mode has no ALB); browse to http://localhost:8080
kubectl -n pointcloud-detection port-forward svc/frontend 8080:80 &
```

### 5. Deploy Worker (After the Service-Account Fix)

Once the dedicated `worker-sa` is confirmed in the chart:

```bash
# Re-deploy backend with worker enabled
helm upgrade backend infrastructure/helm/backend \
    -n pointcloud-detection \
    --reuse-values \
    --set worker.replicas=1 \
    --set worker.gpu.enabled=true
```

> **Cost note**: Enabling the GPU worker spins up a g5 spot node (~$1.00/hr). Set `worker.replicas=0` when not testing.

---

## Integration Test Plan

### Test IT-1: API Health + Upload (No GPU)

**Goal**: Verify API pod runs, S3 upload works, job gets queued to SQS.

```bash
# 1. Port-forward API
kubectl -n pointcloud-detection port-forward svc/backend-api 8000:8000 &

# 2. Health check
curl http://127.0.0.1:8000/health
# Expected: {"status": "healthy", ...}

# 3. Upload a small .bin file
curl -X POST http://127.0.0.1:8000/upload \
    -F "file=@data/kitti_synthetic.bin" \
    -F "config={\"model_mode\":\"kitti\",\"use_intensity\":true}"
# Expected: {"job_id": "...", "status": "queued"}

# 4. Check job was queued
JOB_ID=<from step 3>
curl http://127.0.0.1:8000/jobs/$JOB_ID
# Expected: {"status": "queued", ...} (stays queued until worker picks it up)

# 5. Verify SQS has the message
aws sqs get-queue-attributes \
    --queue-url https://sqs.us-east-1.amazonaws.com/111122223333/pointcloud-detection-dev-jobs \
    --attribute-names ApproximateNumberOfMessages \
    --region us-east-1
# Expected: ApproximateNumberOfMessages > 0
```

**Pass criteria**: Health 200, upload returns job_id, SQS message count > 0.

---

### Test IT-2: Frontend Serves

**Goal**: Frontend loads in the browser. The deploy step above uses port-forward mode,
which has no Ingress or ALB, so the UI is reached through a tunnel.

```bash
# 1. Open the tunnel (skip if the one from the deploy step is still running)
kubectl -n pointcloud-detection port-forward svc/frontend 8080:80 &
UI_URL=http://localhost:8080

# 2. Verify HTML loads
curl -s "$UI_URL" | head -20
# Expected: HTML with <title>Point Cloud Detection</title> or similar
```

3. Open `$UI_URL` in a browser and verify:
   - Upload page renders
   - Model selection dropdown works
   - Dashboard page loads (empty job list is fine)

In public mode, use `https://<INGRESS_HOST>/` instead, from a browser: every request
there needs a Cognito sign-in first, so `curl` gets a redirect, and the ALB's own DNS
name returns 404.

**Pass criteria**: HTML serves, React app loads.

---

### Test IT-3: End-to-End (With Worker + GPU)

**Goal**: Upload → SQS → Worker processes → Results in S3 → API returns detections.

**Prerequisites**: Worker deployed with GPU (`worker.replicas=1, worker.gpu.enabled=true`).

```bash
# 1. Upload a KITTI .bin
curl -X POST http://127.0.0.1:8000/upload \
    -F "file=@data/kitti_synthetic.bin" \
    -F "config={\"model_mode\":\"kitti\",\"use_intensity\":true,\"confidence_threshold\":0.3}"
JOB_ID=<from response>

# 2. Wait for processing (GPU node spin-up: 2-4 min, processing: ~30s)
watch -n 5 "curl -s http://127.0.0.1:8000/jobs/$JOB_ID | python -m json.tool"
# Wait until status changes: queued → processing → completed

# 3. Get results
curl http://127.0.0.1:8000/results/$JOB_ID | python -m json.tool
# Expected: {"detections": [...], "metadata": {...}}

# 4. Verify S3 output
aws s3 ls s3://pointcloud-detection-dev-output-111122223333-us-east-1-an/$JOB_ID/ --region us-east-1
# Expected: detections.json, tiles/ directory
```

**Pass criteria**: Job completes, detections returned, S3 output exists.

---

### Test IT-4: Frontend Upload-to-View Flow

**Goal**: Full browser workflow — upload, wait, view 3D results.

1. Open `http://localhost:8080` in a browser, with the frontend tunnel from IT-2
   running (public mode: `https://<INGRESS_HOST>/`, and sign in)
2. Navigate to Upload page
3. Upload `data/kitti_synthetic.bin` (or a real KITTI velodyne file)
4. Select "KITTI" model mode, 0.3 confidence threshold
5. Click upload — verify progress bar
6. Wait on Dashboard for job to complete
7. Click into completed job — verify 3D viewer loads with point cloud + detection boxes

**Pass criteria**: Upload → processing → view detections — no manual CLI needed.

---

### Test IT-5: Auto-Scaling Verification

**Goal**: Multiple concurrent jobs trigger HPA scale-up.

```bash
# Submit 5 jobs rapidly
for i in $(seq 1 5); do
    curl -s -X POST http://127.0.0.1:8000/upload \
        -F "file=@data/kitti_synthetic.bin" \
        -F "config={\"model_mode\":\"kitti\",\"use_intensity\":true}" > /dev/null
    echo "Submitted job $i"
done

# Watch worker pod count
watch kubectl -n pointcloud-detection get pods -l app=backend-worker

# Check HPA
kubectl -n pointcloud-detection get hpa
```

**Pass criteria**: Worker pods scale from 1 to 2+ when queue depth exceeds `targetQueueDepth`.

---

### Test IT-6: Worker Failure Recovery

**Goal**: Job doesn't get lost if worker crashes.

```bash
# 1. Submit a job
curl -X POST http://127.0.0.1:8000/upload \
    -F "file=@data/kitti_synthetic.bin" \
    -F "config={\"model_mode\":\"kitti\"}"
JOB_ID=<from response>

# 2. Kill the worker pod immediately
WORKER_POD=$(kubectl -n pointcloud-detection get pods -l app=backend-worker -o jsonpath='{.items[0].metadata.name}')
kubectl -n pointcloud-detection delete pod $WORKER_POD

# 3. Wait for replacement pod + SQS visibility timeout to expire (default 300s)
# Job should be reprocessed by the new worker

# 4. Check job eventually completes
sleep 360
curl http://127.0.0.1:8000/jobs/$JOB_ID
# Expected: status = "completed" (retried by new worker)
```

**Pass criteria**: Job completes after worker restart. No data loss.

---

## Known Issues & Workarounds

| Issue | Status | Workaround |
|-------|--------|-----------|
| Worker SA missing SQS Receive/Delete permissions | Fix in progress (dedicated worker-sa) | Deploy with `worker.replicas=0` until fix |
| GPU node takes 2-4 min to spin up from zero | By design (cost optimization) | First job will be slow; subsequent are fast |
| ECR tags are immutable | By design (security) | Always use git SHA as tag |
| IRSA roles are OIDC-scoped per SA name | By design | Can't reuse one SA for both API and worker |

---

## AWS Resource Reference

| Resource | Value |
|----------|-------|
| Cluster | `pointcloud-detection-dev` |
| Region | `us-east-1` |
| Account | `111122223333` |
| Backend ECR | `111122223333.dkr.ecr.us-east-1.amazonaws.com/pointcloud-detection-backend` |
| Frontend ECR | `111122223333.dkr.ecr.us-east-1.amazonaws.com/pointcloud-detection-frontend` |
| Backend IRSA | `arn:aws:iam::111122223333:role/pointcloud-detection-dev-backend-role` |
| Worker IRSA | `arn:aws:iam::111122223333:role/pointcloud-detection-dev-worker-role` |
| S3 Input | `pointcloud-detection-dev-input-111122223333-us-east-1-an` |
| S3 Output | `pointcloud-detection-dev-output-111122223333-us-east-1-an` |
| S3 Tiles | `pointcloud-detection-dev-tiles-111122223333-us-east-1-an` |
| S3 Models | `pointcloud-detection-dev-models-111122223333-us-east-1-an` |
| SQS Queue | `https://sqs.us-east-1.amazonaws.com/111122223333/pointcloud-detection-dev-jobs` |
| Namespace | `pointcloud-detection` |
| GPU Nodegroup | Label: `workload-type=gpu` |

---

## Deployment Order

```
1. [YOU]   Get cluster access (IP allowlist + IAM access entry)
2. [YOU]   Build + push images with git SHA tag
3. [YOU]   Deploy API + Frontend (worker.replicas=0)
4. [YOU]   Run IT-1 (API health) + IT-2 (frontend)
5. [INFRASTRUCTURE] Fix worker-sa chart, confirm fix
6. [YOU]   Deploy worker (worker.replicas=1, gpu.enabled=true)
7. [YOU]   Run IT-3 (end-to-end) + IT-4 (browser flow)
8. [BOTH]  Run IT-5 (scaling) + IT-6 (failure recovery)
```

Steps 1-4 can be done immediately. Steps 5+ are blocked on the service-account fix.
