# Step 4: Run Your First Detection

## Overview
Upload a sample point cloud and run object detection through the web UI.

## Steps

### 4.1 Download Sample Data
Use one scan from Rohbau3D (CC BY 4.0, commercially usable with attribution), an
indoor construction-site dataset: <https://doi.org/10.60776/ZWJFI4>.
```bash
# The scans are per-feature NumPy arrays; the dataset's repository downloads a site
# and converts a scan to E57 (see its README):
git clone https://github.com/RauchLukas/rohbau3d.git
# TODO: pin the exact site/scan and conversion command used in the workshop
```

> **Which model runs.** On the `gpu` and `cpu` images the pretrained models are
> non-commercially licensed and disabled unless the deployment set
> `ALLOW_NONCOMMERCIAL_WEIGHTS=1` (README, "Pretrained weights and licensing"); select a
> model you trained instead (README, "Commercially clean model"). The `stub` image
> (`BUILD_VARIANT=stub`) has no ML stack, downloads no weights, and accepts any model
> selection with placeholder boxes, which is enough to walk through this workflow.

### 4.2 Upload via Web UI
<!-- TODO: Screenshots showing the upload flow -->
1. Open `http://localhost:8080`, with the port-forward from step 3.6 running. (Public
   mode: open `https://<your-hostname>/` and sign in with the Cognito user you created.)
2. Drag and drop the sample file onto the upload zone
3. Select model mode: "Indoor" (for PLY/E57)
4. Click "Upload & Start Detection"

### 4.3 Monitor Job Progress
<!-- TODO: Screenshot of dashboard showing job status -->
- Navigate to Dashboard page
- Watch status change: queued → processing → complete

### 4.4 View Results
<!-- TODO: Screenshot of 3D viewer with detections -->
- Click "View Results" on the completed job
- Explore 3D point cloud with bounding boxes

## Validation Checkpoint
- [ ] File uploads without errors
- [ ] Job reaches `complete` (how long it takes depends on the file's size, the node type,
      and whether a GPU node has to start first; no time is measured for this workshop)
- [ ] Detections visible in 3D viewer
