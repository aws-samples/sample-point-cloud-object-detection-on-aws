# Solution Architecture

High-level view of the Point Cloud Object Detection system on AWS.

> **Publication version:** [`exports/architecture-v2.png`](exports/architecture-v2.png)
> (source: [`src/architecture_v2.py`](src/architecture_v2.py)) is the full version,
> and [`exports/architecture-v2-blog.png`](exports/architecture-v2-blog.png) is its
> 800px cut used as the blog's Figure 1. Both add what this Mermaid view predates: the
> DynamoDB job store, the KEDA and Cluster Autoscaler scaling loop, the NVIDIA device
> plugin, and the numbered request path. Prefer them for anything published.

![Solution architecture](exports/architecture-v2.png)

```mermaid
graph TB
    subgraph Users
        Browser[Web Browser]
    end

    subgraph AWS Cloud
        subgraph EKS Cluster
            subgraph Public Subnet
                ALB[Application Load Balancer]
            end

            subgraph Private Subnet - CPU Nodes
                FE[Frontend Pod<br/>React SPA + nginx]
                API[API Pod<br/>FastAPI]
                Worker[Worker Pod<br/>SQS Consumer]
            end

            subgraph Private Subnet - GPU Nodes
                GPUWorker[GPU Worker Pod<br/>mmdet3d Inference]
                TrainPod[Training Pod<br/>Fine-Tuning]
            end
        end

        subgraph Storage
            S3Input[(S3: Input Bucket<br/>Point Clouds)]
            S3Output[(S3: Output Bucket<br/>Detection Results)]
            S3Tiles[(S3: Tiles Bucket<br/>LOD Tiles)]
            S3Models[(S3: Models Bucket<br/>Fine-Tuned Weights)]
        end

        subgraph Messaging
            SQS[SQS: Job Queue]
            DLQ[SQS: Dead Letter Queue]
        end

        subgraph Security
            KMS[KMS: Encryption Keys]
            IAM[IAM: IRSA Roles]
        end

        subgraph Observability
            CW[CloudWatch<br/>Logs + Metrics + Alarms]
        end
    end

    Browser -->|HTTPS| ALB
    ALB --> FE
    ALB --> API
    API -->|Enqueue job| SQS
    API -->|Upload/Download| S3Input
    API -->|Read results| S3Output
    SQS --> Worker
    SQS --> GPUWorker
    SQS -.->|Failed msgs| DLQ
    Worker -->|Read input| S3Input
    Worker -->|Write results| S3Output
    Worker -->|Write tiles| S3Tiles
    GPUWorker -->|Read input| S3Input
    GPUWorker -->|Write results| S3Output
    TrainPod -->|Read data| S3Input
    TrainPod -->|Write model| S3Models
    KMS -.->|Encrypts| S3Input
    KMS -.->|Encrypts| SQS
    CW -.->|Monitors| API
    CW -.->|Monitors| Worker
```
