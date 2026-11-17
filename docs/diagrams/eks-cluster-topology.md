# EKS Cluster Topology

Network layout showing VPC, subnets, node groups, and pod placement.

```mermaid
graph TB
    subgraph VPC["VPC (10.0.0.0/16)"]
        subgraph PubSubnet1["Public Subnet A (10.0.0.0/20)"]
            ALB[ALB<br/>Ingress Controller]
            NAT1[NAT Gateway]
        end

        subgraph PubSubnet2["Public Subnet B (10.0.16.0/20)"]
            ALB2[ALB Target<br/>Cross-AZ]
            NAT2[NAT Gateway]
        end

        subgraph PrivSubnet1["Private Subnet A (10.0.128.0/20)"]
            subgraph CPUNode1["CPU Node (m5.large)"]
                FE1[frontend-sa<br/>Pod 1]
                API1[backend-sa<br/>Pod 1]
                Worker1[worker-sa<br/>Pod 1]
            end
        end

        subgraph PrivSubnet2["Private Subnet B (10.0.144.0/20)"]
            subgraph CPUNode2["CPU Node (m5.large)"]
                FE2[frontend-sa<br/>Pod 2]
                API2[backend-sa<br/>Pod 2]
                Worker2[worker-sa<br/>Pod 2]
            end
        end

        subgraph GPUSubnet["Private Subnet A (GPU Node Group)"]
            subgraph GPUNode["GPU Node (g5.xlarge) - Scale from Zero"]
                GPUPod[gpu-worker-sa<br/>Pod]
                NVPlugin[NVIDIA Device Plugin]
            end
        end

        subgraph Addons["Cluster Addons (DaemonSets / Deployments)"]
            KEDA[KEDA<br/>queue depth to pods]
            CA[Cluster Autoscaler<br/>pending pods to nodes]
            FB[Fluent Bit]
            ALBC[ALB Controller]
        end
    end

    subgraph AWSServices["AWS Services (VPC Endpoints)"]
        S3EP[S3 Gateway Endpoint]
        SQSEP[SQS Interface Endpoint]
        ECREP[ECR Interface Endpoint]
        STSEP[STS Interface Endpoint]
    end

    SQSEP -->|ApproximateNumberOfMessages<br/>Visible + NotVisible| KEDA
    KEDA -->|scales Deployment 0..10| GPUPod
    GPUPod -.->|Pending: no GPU node| CA
    CA -->|scales node group 0..N| GPUNode
    Internet((Internet)) -->|HTTPS :443| ALB
    ALB -->|Target Group| FE1
    ALB -->|Target Group| FE2
    ALB -->|Target Group| API1
    ALB -->|Target Group| API2
    CPUNode1 -->|Private| S3EP
    CPUNode1 -->|Private| SQSEP
    GPUNode -->|Private| S3EP
    PrivSubnet1 -->|Outbound| NAT1
    PrivSubnet2 -->|Outbound| NAT2
```

## Node Groups

| Node Group | Instance Type | Min | Max | Purpose |
|-----------|--------------|-----|-----|---------|
| cpu-workers | m5.large | 2 | 6 | API, Frontend, Worker (stub) |
| gpu-inference | g5.xlarge | 0 | 4 | mmdet3d inference, fine-tuning |

## Service Accounts (IRSA)

| Service Account | Role | Permissions |
|----------------|------|-------------|
| backend-sa | backend-role | S3 put `uploads/*` (signs presigned uploads), S3 read `results/*` and `tiles/*`, SQS send, DynamoDB, KMS |
| worker-sa | worker-role | S3 read `uploads/*`, S3 put `results/*` and `tiles/*`, SQS consume, DynamoDB, KMS |
| frontend-sa | none | Static nginx; makes no AWS calls |
| training-sa | training-role | S3 RW (input + models), KMS |
| keda-operator (ns `keda`) | keda-operator role | `sqs:GetQueueAttributes` on the job queue only |

## Scaling Control Loop

Two tiers, because neither alone is sufficient — Cluster Autoscaler reacts only to
Pending pods, and nothing else creates pods from queue backlog.

| Tier | Controller | Signal | Range | Timing |
|------|-----------|--------|-------|--------|
| Queue to pods | KEDA `aws-sqs-queue` | `ApproximateNumberOfMessages` visible + in flight | 0 – 10 replicas | 30 s poll, 300 s cooldown, +3 pods/min |
| Pods to nodes | Cluster Autoscaler | Pending pods requesting `nvidia.com/gpu` | 0 – `max_size` per GPU pool | 5 m provision timeout, 5 m scale-down-unneeded |

`scaleOnInFlight: true` is required: a detection job holds its message in flight for
minutes while the visible count sits at 0, so counting only visible messages would
scale the last worker away mid-job.
