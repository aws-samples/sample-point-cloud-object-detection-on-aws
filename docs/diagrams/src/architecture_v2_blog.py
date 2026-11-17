"""Figure 1, blog cut — architecture-v2 condensed to stay legible at 800px.

Regenerate from the repository root:

    uv run --no-project --with diagrams==0.25.1 --with resvg-py==0.5.0 \
        python docs/diagrams/src/architecture_v2_blog.py

Writes docs/diagrams/exports/architecture-v2-blog.{svg,png}. The canvas is 800 units
wide, the AWS blog's maximum image width, so a font size here is the size a reader
sees; nothing is below 13. The PNG is rendered at 2x for high-DPI screens.

Same content as architecture_v2.py, condensed: the four buckets are one S3 node,
the security and operations services are a footer line, and labels are shorter.
Keep the two in step; architecture_v2.py is the repository and docs figure.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from svgkit import DATA, IDENTITY, INK, PLATFORM, SCALE, Canvas  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "exports" / "architecture-v2-blog"

c = Canvas(800, 740)

# ---------------------------------------------------------------- groups ----
c.box(95, 20, 790, 572, "AWS Cloud", stroke=INK, width=2, icon="aws/general/general")
c.box(
    105,
    95,
    560,
    465,
    "VPC",
    stroke="#248814",
    fill="#f7fbf5",
    icon="aws/network/vpc",
    size=14,
    where="top-right",
)
c.box(
    115,
    122,
    550,
    180,
    "Public subnet",
    stroke="#248814",
    fill="#e9f5e4",
    where="top-right",
    size=13,
)
c.box(
    115,
    190,
    550,
    458,
    "Amazon EKS",
    stroke="#ed7100",
    fill="#fff9f2",
    icon="aws/compute/elastic-kubernetes-service",
    where="top-right",
    size=14,
)
c.box(
    125,
    222,
    290,
    345,
    "CPU nodes",
    stroke="#8c4fff",
    fill="#f7f3ff",
    where="bottom-right",
    size=13,
)
c.box(
    300,
    222,
    540,
    345,
    "GPU pools · On-Demand → Spot",
    stroke="#8c4fff",
    fill="#f7f3ff",
    where="top-left",
    size=13,
)
c.box(
    125,
    355,
    540,
    450,
    "Cluster add-ons",
    stroke="#8c4fff",
    dash="5 4",
    where="bottom-right",
    size=13,
)
c.box(
    105,
    478,
    560,
    552,
    "Amazon SQS",
    stroke="#e7157b",
    fill="#fff5f9",
    where="bottom-right",
    size=13,
)

# ----------------------------------------------------------------- nodes ----
c.node(45, 150, "aws/general/user", "User", size=46, font=14)
c.node(
    150,
    150,
    "aws/network/elb-application-load-balancer",
    "ALB · HTTPS · Cognito sign-in\n/api: SPA header, else 403",
    where="right",
    size=40,
    font=14,
    dy=-8,
)

c.node(
    150, 282, "programming/framework/fastapi", "API", where="right", size=40, font=14
)
c.node(245, 282, "programming/framework/react", "Frontend", size=38, font=14)
c.node(350, 282, "k8s/compute/ds", "NVIDIA plugin", size=38, font=14)
c.node(480, 282, "k8s/compute/deploy", "Worker 0–10\ntile · detect", size=42, font=14)

c.node(328, 400, "k8s/clusterconfig/hpa", "KEDA", where="right", size=40, font=14)
c.node(
    500,
    400,
    "aws/management/auto-scaling",
    "Cluster\nAutoscaler",
    where="left",
    size=40,
    font=14,
)

c.node(
    315,
    515,
    "aws/integration/simple-queue-service-sqs-queue",
    "Job queue",
    where="right",
    size=36,
    font=14,
    dy=12,
)
c.node(
    215,
    515,
    "aws/integration/simple-queue-service-sqs-queue",
    "DLQ",
    where="left",
    size=36,
    font=14,
)

c.node(
    620,
    160,
    "aws/storage/simple-storage-service-s3",
    "Amazon S3\ninput · tiles\noutput · models",
    where="right",
    size=44,
    font=14,
    dy=14,
)
c.node(
    620,
    305,
    "aws/database/dynamodb",
    "Amazon\nDynamoDB\njob store",
    where="right",
    size=44,
    font=14,
)

# ----------------------------------------------------- request / data path ----
c.edge([(58, 127), (58, 84), (612, 84), (612, 138)])  # (1) presigned upload
c.badge(250, 84, "1")
c.text(268, 79, "upload scan (presigned URL)", size=13, color=DATA, anchor="start")
# Sign-in, before step 1: Cognito managed login; the ALB then checks the session.
c.node(
    480, 50, "aws/security/cognito", "Amazon Cognito", where="right", size=32, font=14
)
c.edge([(38, 127), (38, 46), (462, 46)], color=IDENTITY, tail=True)
c.text(
    200, 59, "sign in (Cognito managed login)", size=13, color=IDENTITY, anchor="start"
)
c.edge([(160, 130), (160, 64), (462, 64)], color=IDENTITY)
# (4) the viewer loads detections and LOD tiles through the API, which reads S3
c.edge([(642, 142), (785, 142), (785, 560), (140, 560), (140, 303)])
c.badge(660, 515, "4")
c.text(676, 510, "load LOD tiles\nvia the API", size=13, color=DATA, anchor="start")
c.edge([(68, 150), (130, 150)], tail=True)
c.edge([(150, 170), (150, 261)], tail=True)
c.edge([(170, 162), (245, 162), (245, 263)])

# (2) API writes the job record and enqueues it
c.edge([(160, 303), (160, 470), (302, 470), (302, 497)])
c.edge([(302, 470), (590, 470), (590, 314), (598, 314)])
c.badge(160, 400, "2")
c.edge([(297, 521), (233, 521)], color=PLATFORM, dash="5 4", width=1.6)

# (3) worker receives, reads the scan, writes results, tiles and status
c.edge([(333, 507), (545, 507), (545, 292), (501, 292)])
c.badge(545, 400, "3")
c.edge([(501, 272), (570, 272), (570, 160), (598, 160)], tail=True)
c.edge([(501, 282), (580, 282), (580, 300), (598, 300)])

# ---------------------------------------------------- two-tier autoscaling ----
c.edge([(328, 497), (328, 422)], color=SCALE, width=2.6)
c.badge(328, 445, "Tier 1", SCALE)
c.edge(
    [(328, 378), (328, 350), (420, 350), (420, 282), (459, 282)], color=SCALE, width=2.6
)
c.edge([(500, 378), (500, 347)], color=SCALE, width=2.6)
c.badge(500, 364, "Tier 2", SCALE)

# --------------------------------------------------------------- legend ----
y = 605
c.edge([(20, y - 5), (52, y - 5)], halo=False)
c.text(58, y, "Request and data path, steps 1–4", size=13, anchor="start")
c.edge([(300, y - 5), (332, y - 5)], color=SCALE, width=2.6, halo=False)
c.text(338, y, "Autoscaling", size=13, anchor="start")
c.edge([(440, y - 5), (472, y - 5)], color=PLATFORM, dash="5 4", width=1.6, halo=False)
c.text(478, y, "Failed jobs to the dead-letter queue", size=13, anchor="start")
c.edge([(20, y + 21), (52, y + 21)], color=IDENTITY, halo=False)
c.text(
    58,
    y + 26,
    "Sign-in first: every ALB rule needs a Cognito session; /api also needs the SPA "
    "header",
    size=13,
    anchor="start",
)
c.badge(52, y + 52, "Tier 1", SCALE)
c.text(
    92,
    y + 57,
    "KEDA scales workers 0–10 on queue depth, visible + in-flight",
    size=13,
    anchor="start",
)
c.badge(52, y + 78, "Tier 2", SCALE)
c.text(
    92,
    y + 83,
    "Cluster Autoscaler (priority expander) adds up to 10 GPU nodes for Pending "
    "GPU pods",
    size=13,
    anchor="start",
)
c.text(
    20,
    y + 108,
    "Also deployed: Amazon ECR · AWS KMS keys · IAM roles per "
    "workload · CloudWatch · Fluent Bit",
    size=13,
    anchor="start",
    color=PLATFORM,
)
c.text(
    20,
    y + 128,
    "No-domain option: kubectl port-forward mode, with no ALB and no Cognito",
    size=13,
    anchor="start",
    color=PLATFORM,
    italic=True,
)

c.save(OUT)
