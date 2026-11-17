# Architecture

> **Sample code, not a production service.** This solution is provided for
> non-production use. Its security review was done from the code and configuration,
> without a live deployment; the [threat model](../security/THREAT_MODEL.md) lists what
> is fixed, what is accepted, what is deferred and what still needs a live check. Work
> with your security and legal teams before you use it with real data.

## Architecture Diagram
<!-- TODO: insert publication-quality diagram here -->
<!-- Reference: docs/reference/backend-domain-entities.md for the backend data model -->

## Component Description
<!-- TODO: Brief description of each component from the architecture diagram -->

## Data Flow
<!-- TODO: Describe upload → queue → process → results flow -->

## Security Architecture

**Access.** The application contains no login code and stores no credential. Access
is a property of the deployment, chosen with `ACCESS_MODE`:

- **public**: an internet-facing ALB, HTTPS only, runs the Amazon Cognito
  `authenticate-cognito` action on every listener rule. `/api` requests without a
  session get 401, and requests without the `X-PCD-Client` header get 403 from the ALB
  itself, which is the CSRF control for the session cookie. Users are created by an
  operator; self sign-up is off.
- **port-forward**: no load balancer. Users reach the UI and API through
  `kubectl port-forward`, authorised by their IAM identity, an EKS access entry and a
  namespaced Role limited to port-forwarding.

Programmatic access uses the port-forward path, in port-forward mode and in public mode
with `ACCESS_SCOPE=shared`; per-user mode serves only requests signed in through the
ALB. The threat model in
[`docs/security/THREAT_MODEL.md`](../security/THREAT_MODEL.md) records what each gate
does and does not cover.

**Authorization.** In public mode the default is per-user access (`ACCESS_SCOPE`,
threat model T31). The ALB signs each user's claims; the API verifies that signature,
and the signer, app client, user pool and expiry, on every request, as the Elastic Load
Balancing guide requires. Every job, upload, training session, training job and
fine-tuned model records who created it, and each user reaches only their own. Another
user's object is answered as a missing one. With `ACCESS_SCOPE=shared`, and always in
port-forward mode, every signed-in user or port-forward client can read and delete
every job.

**Workload identity.** Each pod that calls AWS has its own IAM role through IRSA: the
api (`backend-sa`), the worker (`worker-sa`), training (`training-sa`), and the Load
Balancer Controller, Cluster Autoscaler, Fluent Bit and KEDA add-ons. The grants are
scoped to prefixes and actions: for example the worker reads only `uploads/*` and writes
only `results/*` and `tiles/*`, and the api may delete only those three prefixes and
job records. No pod mounts a Kubernetes service-account token, and nodes limit instance
metadata to one hop, so pods cannot use the node's role. `deploy.sh` checks each
add-on's logs for credential errors after install.

**Untrusted files.** Uploaded point clouds are parsed by native libraries. On EKS the
worker pod runs them in a separate `parser` container with no AWS credential, no
Kubernetes token and a user of its own, which hands back a fixed-layout point array; the
worker never parses the upload itself. The parser shares the pod's network, so a
compromised parser could still send out the one job's data (T11, T20). The local modes
parse in-process.

**Cluster access.** The EKS API endpoint is private by default. Operators reach it
through an SSM relay that has no cluster access of its own, running kubectl under their
own IAM identity; cluster admins are explicit access entries, not the cluster creator.
Terraform applies are of a plan the operator has seen and confirmed.

**Network policy.** The VPC CNI enforces NetworkPolicy. The application namespace has a
default-deny, and every pod has its own policy: the api admits only the ALB (or, in
port-forward mode, the frontend), the worker and training admit nothing, and every pod's
egress is limited to cluster DNS and HTTPS outside the VPC. Pods run under the Pod
Security `restricted` profile with read-only root filesystems.

**Encryption.** At rest, S3 (SSE-KMS; the access-logs bucket uses SSE-S3, which server
access logging requires), SQS, DynamoDB, EKS secrets, the VPC flow logs, ECR and the
alarm SNS topic use customer managed KMS keys with rotation. The workload and
control-plane log groups use CloudWatch's default encryption, and node volumes the
account's default EBS key. In transit, TLS 1.2 or 1.3 to the ALB, with HSTS, and TLS to
every AWS API; the hop from the ALB to the pods inside the VPC is HTTP (T46, accepted).

**Browser.** An enforcing Content-Security-Policy allows scripts only from the site
itself and uploads only to the input bucket; frames are refused.

## Scaling Architecture

Two-tier: KEDA turns SQS queue depth into worker replicas (0–10), then Cluster
Autoscaler turns the resulting Pending GPU pods into nodes (0–`max_size`). See
[EKS Cluster Topology — Scaling Control Loop](../diagrams/eks-cluster-topology.md#scaling-control-loop)
for signals, ranges, and timings.

<!-- TODO: expand with measured GPU node cold-start numbers and a burst walkthrough -->
