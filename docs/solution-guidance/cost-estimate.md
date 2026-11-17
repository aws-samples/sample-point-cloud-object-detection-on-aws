# Cost Estimate

## Pricing Model
<!-- TODO: Describe components that contribute to cost -->

## Estimated Monthly Costs

These figures are an illustrative estimate, not a quote. They are approximate on-demand
list prices in us-east-1, excluding tax, data transfer beyond what is noted, and any
discounts, and the GPU line uses the on-demand rate as an upper bound for Spot. Prices
change and vary by Region: build your own estimate in the
[AWS Pricing Calculator](https://calculator.aws/) before you deploy. Only the Small tier
has a figure, because only it is broken down below. Larger workloads add CPU and GPU
node hours, storage and logs roughly in proportion; estimate them in the calculator.

| Tier | Workload | Est. Monthly Cost |
|------|----------|-------------------|
| Small | 10 files/day, avg 1GB | ~$500-550 (breakdown below) |
| Medium | 100 files/day, avg 5GB | Estimate in the AWS Pricing Calculator |
| Large | 1000 files/day, avg 10GB | Estimate in the AWS Pricing Calculator |

## Cost Breakdown (Small Tier)
<!-- TODO: Fill in with current pricing -->
| Component | Monthly Cost | Notes |
|-----------|-------------|-------|
| EKS Control Plane | $73 | Fixed |
| CPU Nodes (2x m5.xlarge) | ~$280 | On-demand |
| GPU Nodes (g5.xlarge spot, 2h/day) | ~$60 | Spot pricing |
| NAT Gateway | ~$45 | + data transfer |
| S3 Storage | ~$5-20 | Depends on retention |
| SQS | ~$1 | Per-request |
| CloudWatch | ~$10-30 | Logs + metrics |
| ALB | ~$25 | Fixed + per-request. Public mode only |
| Amazon Cognito | $0 | Public mode only. The Essentials plan includes 10,000 monthly active users |
| AWS KMS keys | ~$6-7 | Six customer managed keys (S3, SQS, DynamoDB, EKS, logs, ECR) at $1 each, plus one for the alarm topic when `alarm_email` is set; plus request charges |
| Systems Manager VPC endpoints | ~$22 | Only with `enable_bastion = true` (the root default; off in the dev tfvars): three interface endpoints (ssm, ssmmessages, ec2messages) in one Availability Zone, at about $0.01 per endpoint-hour in us-east-1, plus a small per-GB charge |
| CloudWatch alarms | ~$0.50 | Queue depth, DLQ and, in public mode, failed sign-ins (a two-metric alarm) |

## Cost Optimization Tips
<!-- TODO: Spot instances, scale-to-zero, lifecycle policies, reserved instances -->

## Calculator Link
No pre-filled estimate is published with this sample. Enter the components in the table
above in the [AWS Pricing Calculator](https://calculator.aws/) for your Region and
workload.
