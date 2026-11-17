# Fine-Tuning Workflow

Process flow from annotation through model training to deployment.

```mermaid
flowchart LR
    subgraph Annotate["1. Annotate"]
        A1[View detection results]
        A2[Enter annotation mode]
        A3[Draw 3D bounding boxes]
        A4[Assign class labels]
        A5[Save annotations]
        A1 --> A2 --> A3 --> A4 --> A5
    end

    subgraph Train["2. Train"]
        T1[Select base model]
        T2[Configure epochs + classes]
        T3[Submit training job]
        T4[GPU pod scheduled]
        T5[Fine-tune with mmdet3d]
        T1 --> T2 --> T3 --> T4 --> T5
    end

    subgraph Evaluate["3. Evaluate"]
        E1[Compute mAP@0.25/0.50]
        E2[Compare to baseline]
        E3[Validate on holdout]
        E1 --> E2 --> E3
    end

    subgraph Register["4. Register"]
        R1[Save weights to S3]
        R2[Record in model registry]
        R3[Tag with metadata]
        R1 --> R2 --> R3
    end

    subgraph Deploy["5. Deploy"]
        D1[Update worker config]
        D2[Rolling restart GPU pods]
        D3[Serve new model]
        D1 --> D2 --> D3
    end

    Annotate --> Train --> Evaluate --> Register --> Deploy
```

## Supported Base Models

| Model | Architecture | Best For | Dataset |
|-------|-------------|----------|---------|
| votenet_scannet | VoteNet | Indoor scenes (doors, furniture) | ScanNet |
| fcaf3d_scannet | FCAF3D | Indoor scenes (dense) | ScanNet |
| pointpillars_kitti | PointPillars | Outdoor (vehicles, barriers) | KITTI/nuScenes |

## Training Data Format

```json
{
  "scene_id": "office-scan-001",
  "point_cloud_path": "s3://<your-input-bucket>/uploads/scan.bin",
  "num_annotations": 5,
  "annotations": [
    {
      "center_xyz": [1.2, 3.4, 0.8],
      "size_wlh": [0.9, 2.1, 2.0],
      "heading": 0.45,
      "class_name": "door"
    }
  ]
}
```

