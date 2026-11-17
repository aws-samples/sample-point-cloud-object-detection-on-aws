# Step 5: Explore Detection Results

## Overview
Deep dive into the 3D viewer, detection details, and segmentation.

## Steps

### 5.1 Navigate the 3D Viewer
<!-- TODO: Document viewer controls (rotate, zoom, pan) -->

### 5.2 Inspect Individual Detections
<!-- TODO: Click detection in sidebar → zoom to object -->

### 5.3 Toggle Segmentation
<!-- TODO: Show segmentation overlay, explain class colors -->

### 5.4 Download Raw Results
```bash
# Detection JSON
aws s3 cp s3://<output-bucket>/results/<job-id>/detections.json ./
# Segmentation JSON
aws s3 cp s3://<output-bucket>/results/<job-id>/segmentation.json ./
```

### 5.5 Understand the Output Format
<!-- TODO: Explain JSON schema briefly, link to full docs -->

### 5.6 Delete a Job and Its Data
When you are done with a job, **Delete** on the dashboard (for complete and failed
jobs) removes its uploaded file, results, tiles, annotations and record. From a
terminal, through the API tunnel:
```bash
curl -X DELETE http://localhost:8000/api/jobs/<job-id>
```
Earlier S3 object versions are removed by a lifecycle rule 7 days later.

## Validation Checkpoint
- [ ] 3D viewer loads without browser issues
- [ ] Detections clickable in sidebar
- [ ] JSON output downloadable and valid
