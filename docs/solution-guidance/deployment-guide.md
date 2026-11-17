# Deployment Guide

## Prerequisites
<!-- TODO: Link to workshop/00-prerequisites.md or inline -->

## Quick Start (Automated)
Choose how people reach the deployment. **Public** (the default) puts the UI on the
internet at your hostname, behind an ALB that signs every user in with Amazon Cognito;
it needs a domain and an ACM certificate.
```bash
cd infrastructure
export ACM_CERT_ARN=<acm-certificate-arn>
export INGRESS_HOST=<hostname-on-the-certificate>
export INGRESS_INBOUND_CIDRS=<optional, comma-separated client CIDRs>
./scripts/deploy.sh dev
```
**Port-forward** needs no domain: nothing is exposed, and users reach the UI through
`kubectl port-forward` with their own IAM identity.
```bash
cd infrastructure
export ACCESS_MODE=port-forward
./scripts/deploy.sh dev
```
The application has no login of its own and ships no API key. In
public mode the script ends by printing the `aws cognito-idp admin-create-user`
command for adding users (self sign-up is off) and the DNS record to create. In either
mode, scripts and CLI tools reach the API with
`kubectl -n pointcloud-detection port-forward svc/api 8000:80`; there are no API keys.
To let other IAM principals in, list them in `app_client_principal_arns` in
`terraform.tfvars`. Each also needs an IAM policy allowing `eks:DescribeCluster` on the
cluster, for `aws eks update-kubeconfig`; the access entry covers Kubernetes calls only.

The ALB passes `x-amzn-oidc-*` headers with the signed-in user's claims. The sample does
not use them. Anything you build on it must verify the signature of `x-amzn-oidc-data`
(and that its `signer` is your ALB) before making any authorization decision on them.
<!-- TODO: Explain what the script does -->

## Manual Deployment Steps
<!-- TODO: Condensed version of workshop steps 1-3 -->

## Configuration Options
<!-- TODO: Document key terraform.tfvars options and Helm values -->

## Troubleshooting
<!-- TODO: Common issues and solutions -->
| Issue | Solution |
|-------|----------|
| EKS cluster creation timeout | Ensure service quota for EKS clusters |
| GPU nodes not scaling | Check Cluster Autoscaler logs, verify GPU AMI |
| ALB not provisioning | Verify ALB controller is installed, check IAM |
| `deploy.sh` stops with "ALLOW_INSECURE_HTTP is no longer supported" | There is no plain-HTTP internet mode: the ALB can sign users in only over HTTPS. Use `ACCESS_MODE=port-forward`, or supply a certificate. |
| Sign-in loops, or the ALB returns 401 after sign-in | Browse to `https://<INGRESS_HOST>/`, not the ALB's DNS name; the Cognito callback is registered for that host only, in lowercase. Sign-in must finish within 15 minutes. |
| UI shows errors and does not return to sign-in when the session ends | The UI reloads to sign in at most once a minute. Reload the page by hand. |
| Uploads fail with a CORS error | Public: use `https://<INGRESS_HOST>`. Port-forward: use exactly `http://localhost:<port_forward_local_port>` (8080 by default) for both the tunnel and the browser. Those are the origins the buckets accept. |
| `aws eks update-kubeconfig` fails with `AccessDeniedException` | The principal needs IAM `eks:DescribeCluster` on the cluster. The access entry from `app_client_principal_arns` covers Kubernetes calls only. |
| `kubectl port-forward` is forbidden | Add the principal to `app_client_principal_arns` and re-apply Terraform. |
| API unreachable after enabling NetworkPolicy | In public mode `networkPolicy.albSourceCidrs` must list the public subnet CIDRs; with target-type ip the ALB connects to pod IPs directly. Both charts must use the same `accessMode`. |
| Workers never scale up despite queue backlog | `kubectl -n pointcloud-detection get scaledobject worker`. `READY=False` means KEDA cannot reach SQS — check `kubectl -n keda logs deploy/keda-operator` for `AccessDenied`, which means the operator SA annotation and the IRSA trust policy disagree on namespace or SA name. |
| Backend install fails: `no matches for kind "ScaledObject"` | KEDA was not installed before the backend chart. Its CRDs must exist first; `deploy.sh` orders this correctly. |
| Worker pod stuck `Pending` with `Insufficient nvidia.com/gpu` | Expected while a GPU node cold-starts. If it persists past ~10 min, check the node group's `k8s.io/cluster-autoscaler/node-template/*` tags — without them CA cannot simulate fit from zero. |
| A GPU node stays up permanently at zero backlog | Something pinned a replica: check `keda.enabled=true` (not `hpa.enabled`) and `keda.minReplicaCount=0`. An HPA cannot scale to zero on EKS. |
