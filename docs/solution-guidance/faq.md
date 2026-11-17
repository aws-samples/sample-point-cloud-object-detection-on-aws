# Frequently Asked Questions

## General

### What point cloud formats are supported?
<!-- TODO: E57, LAS, LAZ, PLY, KITTI .bin — with auto-detection -->

### What objects can the pipeline detect?
<!-- TODO: Indoor (18-class ScanNet), Outdoor (10-class nuScenes), KITTI (3-class) -->

### How large can input files be?
The largest scene measured is a 50 GB synthetic one, which completed within the 16 GB
memory budget in a single run (`PHASE1_COMPLETION.md`, "Memory NFRs"). Peak memory is
bounded by one tile rather than by the scene, by design. 500 GB is the design target and
has not been measured.

## Deployment

### Can I use this without EKS (local only)?
<!-- TODO: Yes — `uv sync && pointcloud-detect detect <file>` -->

### What GPU instances are recommended?
<!-- TODO: g5.xlarge (spot) for inference; g5.2xlarge for training -->

### Can I deploy in a different AWS region?
<!-- TODO: Yes — change region in terraform.tfvars -->

## Fine-Tuning

### What annotation format is required?
<!-- TODO: JSON with center_xyz, size_wlh, heading, class_name -->

### How much training data do I need?
<!-- TODO: Minimum ~50 annotated scenes; 200+ recommended for good results -->

### Can I add custom object classes?
<!-- TODO: Yes — define in training config, model head adjusts automatically -->

## Security

### Is this ready for production?
No. It is sample code for non-production use, reviewed from its code and configuration
without a live deployment. Read the [threat model](../security/THREAT_MODEL.md), which
lists what is fixed, accepted and deferred, and work with your security and legal
teams before you load real data.

### Is data encrypted at rest?
Yes. The input, output, tiles and models buckets use SSE-KMS with a customer managed
key, and so do SQS, DynamoDB, EKS secrets, ECR, the VPC flow logs and the alarm topic.
The S3 access-logs bucket uses SSE-S3, because server access log destinations require
it. The workload and control-plane log groups use CloudWatch's default encryption.

### How are pod permissions managed?
With IRSA: each pod that calls AWS has its own IAM role, scoped to the prefixes and
actions it needs, and no credential is shared or stored. No pod mounts a Kubernetes
service-account token, and the parser that reads uploaded files runs in a container
with no AWS credential at all.

### Can every user see every job?
Not by default. In public mode each signed-in user sees only the jobs, uploads, results,
training jobs and models they created: the API checks the load balancer's signed
sign-in claims on every request. Set `ACCESS_SCOPE=shared` when deploying to let every
user see every job, for a team whose members may all see each other's scans.
Port-forward mode is always shared, because the tunnel carries no user identity.

### Can I rely on the detections?
Treat them as advisory. The models are unmodified public checkpoints with no
adversarial robustness: crafted objects or point perturbations can hide objects from
them or make them see objects that are not there. Keep a person in the loop for any
safety or access decision.

### How do I delete a customer's data?
`DELETE /api/jobs/{id}`, or **Delete** on the dashboard, removes a job's input, results,
tiles, annotations and record. Earlier object versions expire 7 days later, and the
DynamoDB point-in-time recovery window keeps the record for up to 35 days. The README's
*Personal data and retention* section lists every store and the manual steps for an
erasure request. Whether GDPR, CCPA or similar laws apply is the operator's
responsibility.
