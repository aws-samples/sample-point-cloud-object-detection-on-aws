# Step 6: Fine-Tune a Custom Model

## Overview
Train a custom detection model on your own annotated data.

## Steps

### 6.1 Prepare Training Data
<!-- TODO: Document annotation format, tools (SUSTechPOINTS, Segments.ai) -->

### 6.2 Create Training Config
```yaml
# See training_config.example.yaml for full reference
base_model: "fcaf3d_scannet"    # the architecture
base_weights: "scratch"         # default; "pretrained" is non-commercial and opt-in
data_path: "s3://<models-bucket>/training-data/"
epochs: 50
learning_rate: 0.001
```

### 6.3 Run Training Locally (Quick Test)
```bash
uv run python -m pointcloud_detect.training.trainer --config training_config.yaml
```

### 6.4 Submit Training to EKS (Full Dataset)
```bash
# TODO: the chart's arguments do not match the trainer's --config yet, so the Job does
# not start as rendered (see the threat model, M7). The chart needs a GPU image by tag:
helm install training-run infrastructure/helm/training -n pointcloud-detection \
  --set image.repository=<ecr-backend-url> \
  --set-string image.tag=$TAG-gpu \
  --set networkPolicy.vpcCidr=<terraform output vpc_cidr> \
  --set training.dataPath=s3://<models-bucket>/training-data/ \
  --set training.epochs=50
```

### 6.5 Evaluate the Model
```bash
uv run python -m pointcloud_detect.evaluation.evaluator \
  --checkpoint ./training_output/model_final.json \
  --data ./data/annotations/val/
```

### 6.6 Register in Model Registry
<!-- TODO: CLI command or programmatic registration -->

## Validation Checkpoint
- [ ] Training completes without errors
- [ ] Evaluation shows mAP metrics
- [ ] Model registered and loadable via `--mode custom`
