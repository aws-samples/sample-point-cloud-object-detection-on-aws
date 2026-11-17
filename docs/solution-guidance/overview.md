# Solution Overview

<!-- TODO: 2-3 paragraph executive summary -->

## Problem Statement
<!-- TODO: LiDAR object detection at scale — manual annotation is slow, inconsistent, not scalable -->

## Solution Architecture
<!-- TODO: Insert architecture diagram (from docs/diagrams/) -->
<!-- TODO: Describe the EKS-based event-driven pipeline -->

## Key AWS Services Used
<!-- TODO: Table of services + purpose -->
| Service | Purpose |
|---------|---------|
| Amazon EKS | Container orchestration |
| Amazon S3 | Point cloud storage + results |
| Amazon SQS | Job queue (event-driven processing) |
| Amazon ECR | Container image registry |
| Amazon CloudWatch | Monitoring + alerting |

## Deployment Modes
<!-- TODO: Describe local CLI vs EKS modes -->

## Target Audience
- AWS customers implementing point cloud detection
- Construction/AEC professionals needing LiDAR processing pipelines
