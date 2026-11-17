# Step 1: Deploy Infrastructure

## Overview
Provision the EKS cluster, S3 buckets, SQS queue, and supporting resources using Terraform.

## Steps

### 1.1 Clone the Repository
```bash
# TODO: Replace with final GitHub URL
git clone https://github.com/aws-samples/sample-point-cloud-object-detection-on-aws.git
cd sample-point-cloud-object-detection-on-aws
```

### 1.2 Configure Environment
```bash
cd infrastructure
# Edit environments/dev/terraform.tfvars in place. At least set alarm_email; the
# file's comments explain it and the other values. The edit does not need committing:
# deploy.sh only refuses uncommitted changes to what goes into the images.
${EDITOR:-vi} environments/dev/terraform.tfvars
```

### 1.3 Initialize and Apply Terraform
The workshop uses port-forward mode: nothing is exposed to the internet, and you reach
the app through `kubectl` with your own IAM identity, so no domain or certificate is
needed. (With a domain and an ACM certificate you can use
`-var access_mode=public -var app_host=<hostname>` instead, which adds Cognito sign-in;
see step 3.)
```bash
terraform init
terraform plan -out=tfplan -var-file="environments/dev/terraform.tfvars" -var access_mode=port-forward
# Read the plan, then apply exactly what you read
terraform apply tfplan
```

`infrastructure/scripts/deploy.sh` does the same for you: it saves the plan, shows it
and asks before applying it (`AUTO_APPROVE=1` skips the question).

### 1.4 Configure kubectl
The cluster's API server has a private endpoint only, so kubectl reaches it through the
SSM relay the apply created. Terraform prints the commands that start the relay and
write a kubeconfig for it; run them in a second terminal and leave the port-forward
open:
```bash
terraform output -raw bastion_relay_commands
```
Then, with the `KUBECONFIG` they export:
```bash
kubectl get nodes  # Verify: should see CPU nodes ready
```

Terraform gave the identity that ran it a cluster-admin access entry (set
`cluster_admin_principal_arns` to name others). Starting the relay also needs the
policy `terraform output -raw bastion_operator_policy_json` prints, attached to your
role.

## Validation Checkpoint
<!-- TODO: What should the user see to confirm success? -->
- [ ] `terraform apply` completes without errors
- [ ] `kubectl get nodes` shows 2+ nodes in Ready state
- [ ] S3 buckets visible in AWS Console
