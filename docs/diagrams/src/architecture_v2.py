"""Figure 1 — solution architecture, with official AWS icons.

Regenerate from the repository root:

    uv run --no-project --with diagrams==0.25.1 --with resvg-py==0.5.0 \
        python docs/diagrams/src/architecture_v2.py

Writes docs/diagrams/exports/architecture-v2.{svg,png} (PNG at 2x).

Every element is traceable to what the repository deploys: Terraform under
infrastructure/modules with the tfvars deploy.sh applies
(infrastructure/environments/dev/terraform.tfvars), the Helm charts under
infrastructure/helm, and the add-ons installed by infrastructure/scripts/deploy.sh.
Change this file when those change, not when the prose does. Numbered steps match
the numbered list in the blog's Architecture section.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from svgkit import DATA, IDENTITY, INK, PLATFORM, SCALE, Canvas  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "exports" / "architecture-v2"

c = Canvas(1200, 1062)

# ---------------------------------------------------------------- groups ----
c.box(160, 15, 1185, 872, "AWS Cloud", stroke=INK, width=2, icon="aws/general/general")
c.box(172, 45, 1173, 860, "", stroke="#147eba", dash="6 4")
c.text(1163, 67, "AWS Region", size=15, color="#147eba", anchor="end", weight="bold")
c.box(
    185,
    125,
    800,
    667,
    "VPC · 3 Availability Zones",
    stroke="#248814",
    fill="#f7fbf5",
    icon="aws/network/vpc",
    where="top-right",
)
c.box(
    200,
    152,
    785,
    262,
    "Public subnets",
    stroke="#248814",
    fill="#e9f5e4",
    where="top-right",
)
c.box(
    200,
    280,
    785,
    654,
    "Amazon EKS cluster · private subnets",
    stroke="#ed7100",
    fill="#fff9f2",
    icon="aws/compute/elastic-kubernetes-service",
    where="top-right",
)
c.box(
    215,
    312,
    480,
    492,
    "CPU nodes · m5 On-Demand",
    stroke="#8c4fff",
    fill="#f7f3ff",
    where="bottom-right",
    size=14,
    icon="aws/compute/ec2-instances",
)
c.box(
    495,
    312,
    770,
    492,
    "GPU node pools · 0–10 nodes",
    stroke="#8c4fff",
    fill="#f7f3ff",
    where="top-left",
    size=14,
    icon="aws/compute/ec2-instances",
)
c.text(
    505,
    483,
    "On-Demand g4dn → g6 → g5, Spot last",
    size=13,
    color="#8c4fff",
    anchor="start",
    bg=True,
)
c.box(
    215,
    507,
    770,
    642,
    "Cluster add-ons · on CPU nodes",
    stroke="#8c4fff",
    dash="5 4",
    where="bottom-right",
    size=14,
)
c.box(
    185,
    710,
    800,
    818,
    "Amazon SQS",
    stroke="#e7157b",
    fill="#fff5f9",
    icon="aws/integration/simple-queue-service-sqs",
)
c.box(
    830,
    125,
    1160,
    462,
    "Amazon S3 · encrypted, versioned",
    stroke="#3f8624",
    fill="#f5faf2",
    icon="aws/storage/simple-storage-service-s3",
    where="top-right",
)
c.box(
    830,
    478,
    1160,
    572,
    "Amazon DynamoDB",
    stroke="#4053d6",
    fill="#f5f6fe",
    icon="aws/database/dynamodb",
)
c.box(
    830,
    710,
    1160,
    818,
    "Security and operations",
    stroke=PLATFORM,
    fill="#fafafa",
    dash="5 4",
)

# ----------------------------------------------------------------- nodes ----
c.node(80, 205, "aws/general/user", "User\n(web browser +\n3D viewer)", size=60)

c.node(
    255,
    207,
    "aws/network/elb-application-load-balancer",
    "HTTPS · sign-in with\nAmazon Cognito\n/api needs SPA header, else 403",
    where="right",
    font=14,
)
c.node(560, 207, "aws/network/nat-gateway", "NAT gateway", where="right", size=44)

c.node(255, 372, "programming/framework/fastapi", "API\nFastAPI", where="right")
c.node(410, 372, "programming/framework/react", "Frontend\nReact SPA", size=48)
c.node(560, 372, "k8s/compute/ds", "NVIDIA device\nplugin", size=48, font=14)
c.node(700, 372, "k8s/compute/deploy", "Worker, 0–10\ntile · detect · LOD", font=13)

c.node(320, 574, "onprem/logging/fluentbit", "Fluent Bit", where="right", size=44)
c.node(455, 574, "k8s/clusterconfig/hpa", "KEDA\nSQS scaler", where="right", size=48)
c.node(
    730,
    574,
    "aws/management/auto-scaling",
    "Cluster\nAutoscaler",
    where="left",
    size=46,
)

c.node(455, 750, "aws/integration/simple-queue-service-sqs-queue", "Job queue", size=44)
c.node(
    320,
    750,
    "aws/integration/simple-queue-service-sqs-queue",
    "Dead-letter queue",
    size=44,
)

bucket = "aws/storage/simple-storage-service-s3-bucket"
c.node(
    880, 172, bucket, "Input bucket\nraw scans · browser upload", where="right", size=44
)
c.node(
    880,
    252,
    bucket,
    "Tiles bucket\nLOD tiles · read via the API",
    where="right",
    size=44,
    dy=12,
)
c.node(
    880,
    334,
    bucket,
    "Output bucket\ndetections JSON",
    where="right",
    size=44,
    dy=12,
)
c.node(
    880, 416, bucket, "Models bucket\nfine-tuned checkpoints", where="right", size=44
)
c.node(
    880,
    530,
    "aws/database/dynamodb-table",
    "Job store\nstatus · 90-day TTL",
    where="right",
    size=44,
)

platform = [
    (862, 752, "aws/compute/ec2-container-registry", "Amazon ECR"),
    (1010, 752, "aws/security/key-management-service", "AWS KMS keys"),
    (862, 792, "aws/security/identity-and-access-management-iam-role", "IAM (IRSA)"),
    (1010, 792, "aws/management/cloudwatch", "CloudWatch"),
]
for x, y, icon, label in platform:
    c.node(x, y, icon, label, where="right", size=30, font=14)

# ----------------------------------------------------- request / data path ----
# (1) upload straight to S3 over a presigned multipart URL
c.edge([(95, 170), (95, 112), (880, 112), (880, 148)])
c.badge(330, 112, "1")
c.text(348, 106, "presigned multipart upload", size=14, color=DATA, anchor="start")
# Sign-in, before step 1: Cognito managed login, then the ALB checks the session.
c.node(
    680,
    76,
    "aws/security/cognito",
    "Amazon Cognito\nuser pool · managed login",
    where="right",
    size=40,
    font=14,
)
c.edge([(65, 170), (65, 64), (658, 64)], color=IDENTITY, tail=True)
c.text(
    200, 58, "sign in (Cognito managed login)", size=13, color=IDENTITY, anchor="start"
)
c.edge([(262, 181), (262, 88), (658, 88)], color=IDENTITY)
c.text(
    300,
    83,
    "ALB redeems the code and validates the session",
    size=13,
    color=IDENTITY,
    anchor="start",
)
# browser <-> ALB (UI, job submit, status polling, and step 4's reads)
c.edge([(115, 207), (227, 207)], tail=True)
c.text(134, 196, "HTTPS", size=13, color=DATA)
c.edge([(248, 233), (248, 346)], tail=True)
c.text(240, 254, "/api", size=14, color=DATA, anchor="end")
c.edge([(264, 233), (264, 270), (410, 270), (410, 346)])
c.text(337, 264, "/  (UI)", size=14, color=DATA)

# (2) API records the job and enqueues it
c.edge([(266, 398), (266, 696), (440, 696), (440, 728)])
c.badge(266, 540, "2")
c.edge([(440, 696), (818, 696), (818, 540), (858, 540)])
c.text(600, 691, "create job", size=13, color=DATA, bg=True)
c.text(360, 691, "enqueue", size=13, color=DATA, bg=True)
c.edge([(433, 756), (342, 756)], color=PLATFORM, dash="5 4", width=1.6)
c.text(388, 747, "after 3 tries", size=12, color=PLATFORM)

# (3) worker: receive, read scan + weights, write results/tiles/status
c.edge([(477, 744), (778, 744), (778, 455), (632, 455), (632, 392), (674, 392)])
c.badge(778, 600, "3")
c.edge([(858, 172), (806, 172), (806, 358), (726, 358)])  # read scan
c.edge([(726, 366), (816, 366), (816, 246), (858, 246)])  # write tiles
c.edge([(726, 374), (826, 374), (826, 334), (858, 334)])  # write results
c.edge([(858, 416), (846, 416), (846, 382), (726, 382)])  # read weights
c.edge([(726, 390), (836, 390), (836, 522), (858, 522)])  # write status

# (4) the viewer loads detections and LOD tiles through the API, which reads S3
c.edge([(902, 326), (1167, 326)], head=False)
c.edge([(902, 240), (1167, 240), (1167, 836), (246, 836), (246, 398)])
c.badge(700, 836, "4")
c.text(
    718,
    853,
    "viewer loads detections and LOD tiles through the API",
    size=13,
    color=DATA,
    anchor="start",
)

# ---------------------------------------------------- two-tier autoscaling ----
c.edge([(455, 728), (455, 600)], color=SCALE, width=2.6)
c.badge(455, 664, "Tier 1", SCALE)
c.edge(
    [(455, 548), (455, 527), (616, 527), (616, 364), (674, 364)], color=SCALE, width=2.6
)
c.text(540, 521, "scale 0 ↔ 10", size=13, color=SCALE)
c.edge([(730, 550), (730, 494)], color=SCALE, width=2.6)
c.badge(730, 522, "Tier 2", SCALE)

# --------------------------------------------------------------- legend ----
y = 900
c.edge([(170, y), (215, y)], halo=False)
c.text(222, y + 5, "Request and data path, steps 1–4", size=14, anchor="start")
c.edge([(470, y), (515, y)], color=SCALE, width=2.6, halo=False)
c.text(522, y + 5, "Autoscaling control loop", size=14, anchor="start")
c.edge([(740, y), (785, y)], color=PLATFORM, dash="5 4", width=1.6, halo=False)
c.text(792, y + 5, "Failed jobs to the DLQ", size=14, anchor="start")
c.edge([(170, y + 28), (215, y + 28)], color=IDENTITY, halo=False)
c.text(
    222,
    y + 33,
    "Sign-in, before step 1: every ALB rule requires a Cognito session; /api also "
    "requires the SPA's header",
    size=14,
    anchor="start",
)
c.badge(202, y + 62, "Tier 1", SCALE)
c.text(
    242,
    y + 67,
    "KEDA aws-sqs-queue scaler: ApproximateNumberOfMessages, visible + in-flight, "
    "target 2 per pod → worker Deployment 0–10",
    size=14,
    anchor="start",
)
c.badge(202, y + 92, "Tier 2", SCALE)
c.text(
    242,
    y + 97,
    "Cluster Autoscaler (priority expander) adds GPU nodes for Pending GPU pods: "
    "On-Demand g4dn.xlarge, g6.xlarge,\ng5.xlarge and g6.2xlarge pools first, then a "
    "Spot g5 / g4dn pool; each pool scales from 0, up to 10 nodes in total",
    size=14,
    anchor="start",
)
c.text(
    170,
    y + 146,
    "No-domain option: ACCESS_MODE=port-forward deploys no ALB and no Cognito; users "
    "reach the UI and API with kubectl port-forward.",
    size=14,
    anchor="start",
    color=PLATFORM,
    italic=True,
)

c.save(OUT)
