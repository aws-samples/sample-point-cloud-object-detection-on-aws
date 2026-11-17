# Prerequisites

## Required Tools
<!-- TODO: List exact versions and install commands -->
- AWS CLI v2
- Terraform >= 1.10 (for the S3 backend's native locking)
- kubectl
- Helm >= 3.14
- Docker
- Node.js >= 22 (for frontend build)
- Python >= 3.12 + uv (for local mode)

## AWS Account Setup
<!-- TODO: Document required IAM permissions, service quotas (EKS, g5 instances) -->

- **Account-level S3 Block Public Access.** The stack blocks public access on its own
  buckets but leaves the account-wide setting to you. Check it, and turn it on if your
  account policy allows:

  ```bash
  aws s3control get-public-access-block --account-id "$(aws sts get-caller-identity --query Account --output text)"
  ```

- **This is sample code for non-production use.** Use test data, not real customer
  scans, and read the README's *Personal data and retention* section before you upload
  anything that could show people or buildings.

## Estimated Time
- Total workshop: 2-4 hours
- Infrastructure provisioning: ~20 minutes
- Container builds: ~10 minutes
- First detection run: ~5 minutes

## Cost Warning
<!-- TODO: Insert estimated cost ($X/hour while running) and reminder to run cleanup -->
