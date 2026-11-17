# Threat Model: Ingest and Inference Data Path

**Scope**: the point-cloud data path — untrusted file upload, format parsing, streaming
ingest, on-disk tile extracts, inference, and the DynamoDB job store.
**Status**: draft for security review. Authored by hand against the code at
`8672e58`; **not** a completed review and not a substitute for one.
**Companion document**: `aidlc-docs/.threatmodel/threat-model-infrastructure.md`.

> **Current status lives in [`security/THREAT_MODEL.md`](security/THREAT_MODEL.md).**
> That document follows Shostack's four questions. Its
> [threat register](security/THREAT_MODEL.md#232-threat-register) gives the current
> rating and status of T11–T22 and of every later threat (T23 onward). Its
> [mitigation register](security/THREAT_MODEL.md#31-mitigation-register) does the same
> for M11–M22, which keep their meaning, and for M23 onward. Several statuses here are
> out of date since the hardening pass: on EKS the parsers now run in a credential-free
> container (T11, partially fixed), T12 is fixed for LAS as well as E57, T19 is partially
> fixed (the worker checks each message against its job record), and T21 is accepted.
> This file is kept unchanged below as the detailed record
> of the original findings and their evidence at `8672e58`. Its line citations refer to
> that commit, and its residual-risk table predates the mitigations.

> **The companion is not in version control.** `aidlc-docs/` is gitignored, so nothing
> under it is tracked — it exists only in a working copy. That is very likely how it came
> to contradict the Terraform in eight places without anyone noticing: a document nobody
> sees in a merge request cannot drift visibly. This document lives in `docs/` instead,
> so it ships with the repository and changes to it are reviewable. Worth deciding
> whether the companion should move here too.

Citations are `path:line`. Bare Python filenames are relative to
`src/pointcloud_detect/`; Terraform and Helm paths are relative to `infrastructure/`.
Every citation in this document was machine-checked against the tree at `8672e58` —
the cited range must exist and contain the construct it is cited for.

## Why this document exists

The companion threat model is infrastructure-scoped. Its nine components are the
ALB, EKS control plane, the three pod types, the VPC, IRSA roles, ECR and CloudWatch;
its five data stores are the four S3 buckets and the SQS queue pair. It contains no
DynamoDB table, no local disk, no streaming or extract concept, and **no threat
relating to file parsing at all** — the single largest untrusted-input surface in the
system.

That is not a defect in that document. The streaming ingest path
(`src/pointcloud_detect/streaming.py`, 1668 lines) and the store-once on-disk extracts
post-date it, as does the DynamoDB job store. This document covers what it does not.

IDs continue from the companion rather than restarting, so the two can be read
together: components from **C010**, data stores from **D006**, assets from **A008**,
flows from **F005**, threats from **T11**, mitigations from **M11**.

### Reconciling the companion document

The companion describes the **pre-mitigation** state and now contradicts the
Terraform in several places. A reviewer reading both will hit those contradictions, so
they are listed here rather than left to be discovered. The companion is
machine-generated and would lose hand edits on regeneration, which is why the
reconciliation lives in this file.

| Companion claim | Current state | Evidence |
|---|---|---|
| D001-D004 "SSE-AES256" | SSE-KMS with a customer-managed key, `bucket_key_enabled` | `modules/s3/main.tf:148-161` |
| D005 SQS "NO encryption at rest" | CMK-encrypted, both queue and DLQ | `modules/sqs/main.tf:7-8, 19-20` |
| "no TLS-enforce policy" | `aws:SecureTransport=false` deny on buckets and both queues | `modules/s3/main.tf:216-235`, `modules/sqs/main.tf:34-77` |
| "no access logging, no versioning" | Both enabled on all four data buckets | `modules/s3/main.tf:5-8, 139-146, 174` |
| "no flow logs" | Implemented | `modules/vpc/flow-logs.tf` |
| "no launch template enforcing IMDSv2" | `http_tokens = required`, `hop_limit = 1`, encrypted EBS | `modules/eks/launch-template.tf` |
| "no envelope encryption_config" | Dedicated EKS CMK | `modules/eks/main.tf:65`, `modules/kms/main.tf:105-113` |
| T1 "endpoint_public_access=true, open to 0.0.0.0/0" | Private by default; two `precondition`s reject an empty CIDR list and `0.0.0.0/0` | `modules/eks/main.tf:49-61, 84-92`, `variables.tf:126-142` |

Two companion findings remain **open**, and are restated here because they are on this
data path: the wildcard CORS policy (T5) and the absence of any API authentication,
which the companion implies via `Authentication Requirement: Federated` but never
raises as a threat. See T16.

## Table of Contents

1. [System Architecture](#system-architecture)
2. [Trust Boundaries](#trust-boundaries)
3. [Assets and Flows](#assets-and-flows)
4. [Threats](#threats)
5. [Mitigations](#mitigations)
6. [Assumptions](#assumptions)
7. [Residual Risk Summary](#residual-risk-summary)

## System Architecture

### Components

| ID | Name | Type | Service Provider | Description |
|---|---|---|---|---|
| C010 | Streaming Ingest | Process | Self | Two-pass chunked ingest for LAS/LAZ/E57 keeping peak RAM bounded by one tile; `streaming.py:937-1348` |
| C011 | Tile Extract Store | Data Process | Self | Per-invocation on-disk store of routed points, one file set per planned leaf; `streaming.py:1515-1637` |
| C012 | Native Format Parsers | Library | Third party | laspy with the `lazrs` LAZ decompressor, pye57 over SWIG to libE57/Xerces, trimesh, raw `np.fromfile`; `io_readers.py`, `streaming.py` |
| C013 | DynamoDB Job Store | Data Store Client | AWS | Shared job state replacing the process-local dict; `api/store.py:182-354` |
| C014 | Model Registry and Checkpoint Loader | Process | Self | Resolves a requested model to a checkpoint path, loaded via `mmdet3d.apis.init_model`; `worker/processor.py:169-183`, `backends/mmdet3d.py:315, 463` |
| C015 | Fluent Bit Log Shipper | Observability | AWS | DaemonSet shipping pod logs to CloudWatch Logs under `/aws/eks/<cluster>/workloads` |

### Data Stores

| ID | Name | Type | Classification | Encrypted at Rest | Description |
|---|---|---|---|---|---|
| D006 | DynamoDB jobs table | NoSQL | Confidential | Yes — project CMK | Job records including filename, S3 key, config and `error_message`; `modules/dynamodb/main.tf:15-30` |
| D007 | Node ephemeral disk | Local filesystem | Confidential | Partially — account default EBS key, **not** a project CMK | Holds the downloaded input file and every tile extract; `modules/eks/launch-template.tf:19-21, 49-51` |
| D008 | CloudWatch Logs | Log store | Internal | Yes — CMK | Worker and API logs, including failure reasons; `modules/kms/main.tf:150-158` |

## Trust Boundaries

Adds one boundary the companion does not model.

#### Parser Boundary

- **Type**: BoundaryType.PROCESS
- **Controls**: header validation, cell and tile caps, disk headroom check
- **Description**: The boundary between an untrusted binary file and the native
  libraries that parse it. Crossed inside the worker pod, in-process, with the worker's
  IRSA credentials resolvable from the same process. This is the boundary with the
  weakest controls and the highest consequence, and the companion does not model it.

#### Node Disk Boundary

- **Type**: BoundaryType.RESOURCE
- **Description**: Extracts and downloaded inputs from every concurrently running job
  share one node root volume, with no per-job quota and no `ephemeral-storage` limit.
  Tenancy between jobs is a boundary, and it is currently unenforced.

## Assets and Flows

### Assets

| ID | Name | Type | Classification | Sensitivity | Criticality | Owner |
|---|---|---|---|---|---|---|
| A008 | Customer point-cloud geometry | AssetType.DATA | AssetClassification.CONFIDENTIAL | 4 | 4 | Customer |
| A009 | Model checkpoints | AssetType.DATA | AssetClassification.INTERNAL | 3 | 4 | Operator |
| A010 | Job metadata and error messages | AssetType.DATA | AssetClassification.INTERNAL | 2 | 3 | Operator |
| A011 | Worker IRSA web identity token | AssetType.CREDENTIAL | AssetClassification.RESTRICTED | 5 | 5 | Operator |

### Asset Flows

| ID | Asset | Source | Destination | Protocol | Encrypted | Risk Level |
|---|---|---|---|---|---|---|
| F005 | A008 | Browser | S3 input bucket | HTTPS presigned PUT | Yes | 3 |
| F006 | A008 | S3 input bucket | C010 via node disk (D007) | HTTPS then local write | In transit yes; at rest account-default key | 4 |
| F007 | A008 | C010 | C011 (D007) | Local file write, plaintext | **No application-level encryption** | 4 |
| F008 | A008 | C012 | Inference backend | In-process | N/A | 5 |
| F009 | A010 | Worker | D006 | HTTPS | Yes | 2 |
| F010 | A010 | Worker | D008 | HTTPS | Yes | 2 |
| F011 | A011 | EKS projected volume | AWS STS | HTTPS | Yes | 5 |

## Threats

### Identified Threats

#### T11: Unauthenticated uploader reaching a native parser

**Statement**: An unauthenticated remote user can upload a malformed or hostile
point-cloud file and have it parsed in-process by native code in the worker pod, which
leads to a memory-safety class compromise of a process holding credentials.

- **Prerequisites**: No authentication on `/api/upload` or `/api/jobs` (see T16);
  `detect_format` decides handling from the first 16 bytes and otherwise the filename
  extension, never validating internal structure (`io_readers.py:17-50`).
- **Action**: The worker downloads the object to local disk and hands it to laspy with
  the `lazrs` native LAZ decompressor, or to pye57 over SWIG to libE57 and Xerces, or to
  trimesh. Call sites: `io_readers.read_las`, `read_e57`, `read_ply`;
  `streaming.py:57-121, 139-202, 219-361`.
- **Impact**: Native memory corruption in a process whose IRSA credentials can read the
  input bucket and write the output and tiles buckets. This is the highest-consequence
  threat on the path and has no compensating control today.
- **Impacted Assets**: A008, A011, D007

#### T12: Header-driven allocation before validation

**Statement**: An attacker supplying a file whose header declares far more points than
it contains can cause an allocation sized from that untrusted number, which leads to
worker memory exhaustion.

- **Prerequisites**: E57 buffer capacity is derived from the file's own
  `header.point_count` (`io_readers.py:72` declared count, `103` buffer allocation); occupancy grid sizing derives from the
  header extent (`streaming.py:514-570`).
- **Action**: Declare a point count or extent far larger than reality and submit the job.
- **Impact**: Worker OOM. Because nothing in the cluster path marks a job failed on OOM
  kill, the job is retried, so a single file can evict workers repeatedly.
- **Impacted Assets**: D007
- **Existing partial controls**: `MAX_OCCUPANCY_CELLS = 2_000_000`
  (`streaming.py:432`), `_occupancy_shape` shrink loop (`481-499`), `MAX_PLANNED_TILES`
  (`437`). These bound the grid and tile count but **not** the E57 buffer allocation.

#### T13: Silent result truncation via a patched header

**Statement**: An attacker able to submit a file with internally consistent but
incorrect header bounds can cause most of its points to be excluded from inference
while the job reports success, which leads to silently wrong results.

- **Prerequisites**: Tile planning partitions exactly the box it is given, and
  `read_region_*` only reads inside it (`streaming.py:449-455`).
- **Action**: Patch header bounds so that real points fall outside every planned extent.
- **Impact**: Measured on an otherwise valid 40,000-point file with only its header
  bounds patched: **75.1% of points were never read and the run still exited 0**
  (`streaming.py:451-455`). Integrity, not availability — the output looks legitimate.
- **Impacted Assets**: A008
- **Existing partial controls**: `validate_scene_bounds` (`440-479`) rejects NaN, inf,
  inverted, zero-count and degenerate bounds; the planning box is the union of header
  and observed data bounds with a `RuntimeWarning` when the header is stale
  (`988-1005`). A warning is not a failure, and nothing surfaces it to the user.

#### T14: Customer geometry at rest outside the CMK boundary

**Statement**: An actor with access to a node's EBS volume or a snapshot of it can read
customer point-cloud geometry that is encrypted only by the account default EBS key,
which leads to disclosure outside the key boundary applied everywhere else.

- **Prerequisites**: Extracts are written as raw `ndarray.tobytes()` with no
  application-level encryption (`streaming.py:1434-1460`); the node root volume sets
  `encrypted = "true"` with **no `kms_key_id`**, so it uses the account default EBS key
  rather than one of the five project CMKs (`modules/eks/launch-template.tf:19-21, 49-51`).
- **Action**: Read the volume, or restore a snapshot of it, using default-key access.
- **Impact**: Every other store on this path — S3, SQS, DynamoDB, EKS secrets — is
  CMK-gated with rotation enabled. Both the downloaded input file and all derived
  extracts sit outside that boundary, so the key policy controlling access to customer
  geometry differs from the documented posture.
- **Impacted Assets**: A008, D007

#### T15: Unbounded node disk consumption

**Statement**: A user submitting one very large or several concurrent jobs can exhaust
the node root volume, which leads to eviction of co-tenant pods.

- **Prerequisites**: `PCD_EXTRACT_DIR` is **never set** in the chart, Dockerfiles or
  compose files, so extracts land in the system temp directory on the node root volume.
  The worker deployment declares no `volumes`, no `emptyDir` and **no
  `ephemeral-storage` request or limit**. Node root volume is 100 GiB
  (`modules/eks/launch-template.tf:19`).
- **Action**: Submit a job whose extract footprint approaches the volume size, or
  several jobs concurrently.
- **Impact**: Disk pressure evicts pods on the node, including pods belonging to other
  jobs. Kubernetes cannot schedule around a limit that is not declared.
- **Impacted Assets**: D007
- **Existing partial controls**: a pre-flight headroom check compares an estimated
  footprint against `shutil.disk_usage(...).free` (`streaming.py:1070-1081`), and
  buffering is bounded by `EXTRACT_FLUSH_BYTES` 256 MiB (`1374`) with a 512 MiB read
  cache (`1551`). Two gaps: the estimate counts only the current job, so concurrent
  jobs are invisible to it; and if `shutil.disk_usage` raises `OSError` the check is
  **skipped entirely** (`1071-1076`).

#### T16: Unauthenticated API on an internet-facing listener

**Statement**: Any remote user can drive the entire job lifecycle without
authenticating, which leads to unauthorised use, disclosure of others' results, and
free minting of S3 upload credentials.

- **Prerequisites**: No authentication on any route — a search for `Depends`,
  `HTTPBearer`, `api_key`, `Authorization` or `Security(` across
  `api/routes/` returns **zero matches**. CORS is `allow_origins=["*"]` with
  `allow_credentials=True` (`api/app.py:23-29`). The ALB is
  `scheme: internet-facing` with **no TLS annotation and no host rule**
  (`helm/frontend/templates/ingress.yaml:11-13`, `helm/frontend/values.yaml:23-29`).
- **Action**: Call `/api/upload` to mint a presigned S3 PUT URL valid for 3600s
  (`api/deps.py:23`), `/api/jobs` to enqueue work, `/api/results` to read any job's
  output. Eight routers are mounted unauthenticated (`api/app.py:31-42`).
- **Impact**: Unauthorised compute consumption on GPU nodes, disclosure of other
  users' detections, and a write primitive into the input bucket. Also the entry
  condition for T11, T18 and T17.
- **Impacted Assets**: A008, A010

#### T17: Unauthenticated job status write

**Statement**: Any remote user can set any job's status and error text, which leads to
falsified job state.

- **Prerequisites**: `PUT /api/jobs/{job_id}/status` takes `status` and
  `error_message` as query parameters with no authentication
  (`api/routes/jobs.py:79-101`). It exists as the worker's callback for the in-memory
  backend but is reachable by anyone.
- **Action**: Mark a running job `complete`, or mark a completed job `failed` with
  chosen error text.
- **Impact**: A victim sees a job as failed, or as complete with no detections. The
  supplied `error_message` is persisted (`api/store.py:97-98`) and rendered in the
  dashboard.
- **Impacted Assets**: A010, D006

#### T18: Attacker-controlled checkpoint path

**Statement**: An unauthenticated user can specify an arbitrary weights path in a job
submission and have it passed to a checkpoint loader, which leads at minimum to model
substitution and potentially to code execution.

- **Prerequisites**: The SQS `config` dict is applied wholesale
  (`worker/processor.py:134-190`). `custom_weights_version` is resolved via the model
  registry, and on a registry miss the **else branch treats the value as a direct path
  to weights** (`worker/processor.py:176-183`). That path reaches
  `mmdet3d.apis.init_model(config_path, checkpoint, ...)`
  (`backends/mmdet3d.py:315, 463`), which loads via `torch.load` underneath.
- **Action**: Submit a job with `custom_weights_version` set to a chosen filesystem
  path.
- **Impact**: Inference runs against a different model than intended, or errors in a
  way that leaks path information. PyTorch checkpoints are pickle-based, so a path the
  attacker also controls the contents of would be an execution primitive — that
  additionally requires placing a file at a worker-readable path, which this threat
  does not by itself establish. Stated deliberately as a path-control finding rather
  than a confirmed RCE.
- **Impacted Assets**: A009, A011

#### T19: Worker write scope is bucket-wide

**Statement**: A compromised worker can overwrite any job's results, not only its own,
which leads to cross-job tampering.

- **Prerequisites**: The worker policy grants `s3:PutObject` on
  `${output_bucket_arn}/*` and `${tiles_bucket_arn}/*` with no per-job prefix condition
  (`modules/iam/main.tf:86-89`).
- **Action**: Write to another job's result or tile keys.
- **Impact**: Falsified detections attributed to another job. Bucket versioning
  (`modules/s3/main.tf:139-146`) preserves the original but does not prevent the
  overwrite being served.
- **Impacted Assets**: A008, A010

#### T20: Unrestricted egress from worker pods

**Statement**: A compromised worker can open arbitrary outbound connections, which
leads to exfiltration of customer geometry.

- **Prerequisites**: The chart ships exactly one NetworkPolicy, `allow-ingress-to-api`,
  with `policyTypes: [Ingress]` only. **No policy selects worker pods, and there are no
  egress rules anywhere** (`helm/backend/templates/networkpolicy.yaml:1-24`).
- **Action**: Send point-cloud data or credentials to an external endpoint.
- **Impact**: Exfiltration with no network-layer control to stop it. Raises the impact
  of T11 and T18 from local compromise to data loss.
- **Impacted Assets**: A008, A011

#### T21: AWS endpoint redirection via environment

**Statement**: An actor able to influence worker environment variables can redirect all
AWS API calls to an endpoint they control, which leads to credential capture.

- **Prerequisites**: `AWS_ENDPOINT_URL` is honoured by the worker's boto3 clients
  (`worker/consumer.py:21`) and by the API (`api/deps.py:14, 35-36`).
- **Action**: Set `AWS_ENDPOINT_URL` — via the ConfigMap, the chart values, or a
  compromised deploy path — to an attacker-controlled host.
- **Impact**: Signed requests, and the STS exchange, are directed to an attacker
  endpoint. Note this is a deploy-time integrity concern rather than a runtime one for
  an unprivileged remote attacker.
- **Impacted Assets**: A011

#### T22: Unscoped CloudWatch metric permission

**Statement**: A compromised worker can publish metrics into any namespace, which leads
to pollution of operational signals.

- **Prerequisites**: `cloudwatch:PutMetricData` on `"*"` — the only unscoped statement
  in the worker policy (`modules/iam/main.tf:94-97`).
- **Action**: Publish misleading metric data.
- **Impact**: Distorted dashboards and alarms, including any KEDA or autoscaling signal
  keyed off custom metrics. Low impact, listed for completeness because it is the one
  deviation from resource scoping in an otherwise well-scoped policy set.

## Mitigations

Numbered to correspond one-to-one with T11-T22, matching the companion's convention.

#### M11: Parse untrusted point-cloud files in an isolated context — a separate process with a restricted seccomp profile, a memory cap and no credentials — rather than in the worker process that holds IRSA credentials.

#### M12: Bound E57 buffer allocation by the actual file size rather than the header's declared `point_count`, applying the same defensive treatment already given to grid and tile counts.

#### M13: Promote the stale-header condition from a `RuntimeWarning` to a recorded job outcome, so a run that excluded points is visibly degraded rather than indistinguishable from a clean one.

#### M14: Give the node volumes an explicit project CMK via `kms_key_id` on the launch template block device, bringing derived customer geometry inside the same key boundary as every other store.

#### M15: Declare `ephemeral-storage` requests and limits on the worker, mount a dedicated `emptyDir` or volume for extracts, set `PCD_EXTRACT_DIR` to it in the chart, and fail closed rather than skipping the headroom check when `disk_usage` raises.

#### M16: Require authentication on every route, restrict CORS to known origins, and terminate TLS at the ALB with a host rule rather than serving an open HTTP listener matching any Host.

#### M17: Restrict the status callback to the worker's identity, or remove it entirely now that the DynamoDB store makes the HTTP callback unnecessary for multi-replica deployments.

#### M18: Reject `custom_weights_version` values that do not resolve in the model registry, rather than falling back to treating the value as a filesystem path.

#### M19: Scope the worker's `s3:PutObject` to a per-job key prefix so a job cannot write outside its own results.

#### M20: Add an egress NetworkPolicy for worker pods allowing only the AWS endpoints they need, and an ingress policy that selects them.

#### M21: Remove `AWS_ENDPOINT_URL` support from production images, or gate it behind an explicit development flag.

#### M22: Scope `cloudwatch:PutMetricData` with a `cloudwatch:namespace` condition.

## Assumptions

1. The deployment is the one this repository ships. Several findings depend on shipped
   defaults — an internet-facing ALB, no set `PCD_EXTRACT_DIR`, no declared
   `ephemeral-storage` — and an operator who has changed those has a different
   exposure.
2. Uploaded point-cloud files are treated as fully untrusted. This is the assumption
   the companion document does not make, and it is what surfaces T11 through T13.
3. Worker pods run with no network egress restriction, per the shipped chart.
4. No finding here has been validated by exploitation. T11 in particular asserts a
   threat class from the presence of native parsers on an untrusted path, not a
   demonstrated vulnerability in laspy, pye57, libE57 or trimesh.
5. Threat actor archetypes and trust zones are inherited from the companion document
   rather than restated.

## Residual Risk Summary

Ranked by consequence, for triage rather than as a formal rating.

| ID | Finding | Consequence | Entry condition |
|---|---|---|---|
| T16 | No API authentication, internet-facing, no TLS | Enables T11, T17, T18 | None |
| T11 | Untrusted binary to native parsers in a credentialed process | Memory-safety compromise | T16 |
| T18 | Attacker-controlled checkpoint path | Model substitution; possible execution | T16 |
| T20 | No egress restriction on workers | Turns local compromise into data loss | T11 or T18 |
| T13 | Silent truncation, exit 0 | Wrong results that look right | T16 |
| T15 | No disk quota | Co-tenant eviction | T16 |
| T14 | Geometry at rest outside the CMK boundary | Disclosure via volume or snapshot | Node or snapshot access |
| T12 | Header-driven allocation | Worker OOM, repeated on retry | T16 |
| T17 | Unauthenticated status write | Falsified job state | T16 |
| T19 | Bucket-wide worker writes | Cross-job tampering | T11 or T18 |
| T21 | Endpoint redirection | Credential capture | Deploy-path compromise |
| T22 | Unscoped `PutMetricData` | Distorted metrics | T11 or T18 |

**T16 is the root.** Nine of the twelve findings list it as their entry condition.
Addressing authentication and the listener first collapses most of this table, and
nothing else here is worth sequencing ahead of it.

Note that the four items under `Existing partial controls` above — bounds validation,
cell and tile caps, the flush and read-cache limits, and the headroom check — are
genuine and were clearly written with hostile input in mind. The gaps are specific,
not a general absence of care.
