# Step 2: Build and Push Container Images

## Overview
Build Docker images for the backend and frontend, push to ECR.

## Steps

### 2.1 Authenticate with ECR
```bash
# TODO: Use actual account ID and region from Terraform output
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
```

The repositories are immutable, so each image is tagged with the commit it was built
from, as `infrastructure/scripts/build-push.sh` does. The charts refuse `latest`.

```bash
export TAG=$(git rev-parse --short=12 HEAD)
```

### 2.2 Build Backend Image
```bash
docker build -t <ecr-url>/pointcloud-detection-backend:$TAG -f Dockerfile .
docker push <ecr-url>/pointcloud-detection-backend:$TAG
```

### 2.3 Build Frontend Image
```bash
docker build -t <ecr-url>/pointcloud-detection-frontend:$TAG ./frontend
docker push <ecr-url>/pointcloud-detection-frontend:$TAG
```

## Validation Checkpoint
- [ ] Both images pushed to ECR successfully
- [ ] Images visible in ECR console with scan results
