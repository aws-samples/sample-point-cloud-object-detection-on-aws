# Step 7: Cleanup

## Overview
Tear down all AWS resources to avoid ongoing charges.

## Steps

### 7.1 Remove Helm Releases
```bash
helm uninstall pointcloud-backend -n pointcloud-detection
helm uninstall pointcloud-frontend -n pointcloud-detection
kubectl delete namespace pointcloud-detection
```

### 7.2 Destroy Infrastructure
```bash
cd infrastructure
./scripts/destroy.sh dev
```

`destroy.sh` shows the destroy plan and asks before it removes anything. It leaves the
account-wide S3 Block Public Access untouched: if Terraform manages it
(`manage_account_public_access_block = true`), the script takes it out of state first,
so tearing the stack down never switches it off.

The buckets are versioned, and S3 deletes no bucket that still holds an object version,
so once you confirm the plan `destroy.sh` empties them, every version and delete marker
included, before it destroys anything. If the destroy stops part-way, run it again: it
plans what is left and asks again. The logs bucket can fail this way when S3 delivers
access logs into it after it was emptied.

### 7.3 Verify Cleanup
- [ ] EKS cluster deleted
- [ ] S3 buckets deleted
- [ ] No running EC2 instances from node groups
- [ ] NAT Gateway deleted
- [ ] Load Balancer deleted (public mode only; the workshop's port-forward mode creates none)
- [ ] CloudWatch log groups: `destroy.sh` ends by listing the ones the destroy does not
  remove, each with the command that deletes it. EKS creates
  `/aws/eks/<cluster>/cluster` for the control-plane logs and Fluent Bit creates
  `/aws/eks/<cluster>/workloads`, outside Terraform, and the flow-log service can
  recreate `/vpc/<cluster>/flow-logs` from its last records just after the destroy
  deletes it.
- [ ] KMS keys: the stack's keys are scheduled for deletion after a 30-day waiting period.

## Cost Note
The log groups `destroy.sh` lists keep their logs, and their storage is billed, until you
delete them. The control-plane group has no retention set, so it never expires on its
own. AWS KMS does not charge for keys that are scheduled for deletion. The stack creates
no Route 53 resources.
