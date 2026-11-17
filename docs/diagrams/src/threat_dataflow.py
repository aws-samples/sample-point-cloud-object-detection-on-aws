"""Threat model data-flow diagram — trust boundaries, flows and threat status.

Regenerate from the repository root:

    uv run --no-project --with diagrams==0.25.1 --with resvg-py==0.5.0 \
        python docs/diagrams/src/threat_dataflow.py

Writes docs/diagrams/exports/threat-dataflow.{svg,png} (PNG at 2x).

Companion to docs/security/THREAT_MODEL.md, drawn against the public access mode that
document reviews: TLS and Amazon Cognito sign-in at the ALB, the ALB's signed claims
verified by the application for per-user access, enforced NetworkPolicy,
prefix-scoped IRSA grants. Blue numbers are that document's flow IDs. REGISTER mirrors
its threat register (severity, status); when the register changes, change REGISTER in
the same commit.

The threats that were High or Medium before the hardening pass and sit on a drawn
component are badged, plus the Low ones that describe the sign-in design. Everything
else is listed in the legend.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from svgkit import (  # noqa: E402
    DATA,
    FIXED,
    PARTIAL,
    PLATFORM,
    THREAT,
    Canvas,
)

OUT = Path(__file__).resolve().parents[1] / "exports" / "threat-dataflow"

# docs/security/THREAT_MODEL.md, section 2.3.2: ID -> (severity, status).
# Status: fixed, partial (partially fixed, including a part accepted), open (any
# qualifier), accepted (accepted in writing, 3.3). T68 is deliberately unused there.
REGISTER = {
    "T5": ("Low", "fixed"),
    "T11": ("Medium", "partial"),
    "T12": ("Low", "fixed"),
    "T13": ("Medium", "open"),
    "T14": ("Low", "open"),
    "T15": ("Low", "fixed"),
    "T16": ("Low", "fixed"),
    "T17": ("Low", "fixed"),
    "T18": ("Low", "fixed"),
    "T19": ("Medium", "partial"),
    "T20": ("Medium", "partial"),
    "T21": ("Low", "accepted"),
    "T22": ("Low", "fixed"),
    "T23": ("Low", "fixed"),
    "T24": ("Low", "fixed"),
    "T25": ("Low", "fixed"),
    "T26": ("Low", "fixed"),
    "T27": ("Low", "fixed"),
    "T28": ("Low", "fixed"),
    "T29": ("Low", "fixed"),
    "T30": ("Low", "fixed"),
    "T31": ("Low", "fixed"),
    "T32": ("Low", "fixed"),
    "T33": ("Medium", "partial"),
    "T34": ("Low", "fixed"),
    "T35": ("Low", "fixed"),
    "T36": ("Low", "accepted"),
    "T37": ("Low", "fixed"),
    "T38": ("Low", "fixed"),
    "T39": ("Low", "open"),
    "T40": ("Low", "partial"),
    "T41": ("Low", "fixed"),
    "T42": ("Low", "fixed"),
    "T43": ("Medium", "partial"),
    "T44": ("Low", "fixed"),
    "T45": ("Medium", "partial"),
    "T46": ("Low", "accepted"),
    "T47": ("Medium", "open"),
    "T48": ("Low", "fixed"),
    "T49": ("Low", "fixed"),
    "T50": ("Low", "partial"),
    "T51": ("Low", "fixed"),
    "T52": ("Low", "partial"),
    "T53": ("Medium", "partial"),
    "T54": ("Low", "fixed"),
    "T55": ("Low", "partial"),
    "T56": ("Low", "fixed"),
    "T57": ("Low", "partial"),
    "T58": ("Low", "partial"),
    "T59": ("Low", "fixed"),
    "T60": ("Medium", "partial"),
    "T61": ("Low", "fixed"),
    "T62": ("Low", "partial"),
    "T63": ("Low", "fixed"),
    "T64": ("Low", "partial"),
    "T65": ("Low", "accepted"),
    "T66": ("Medium", "partial"),
    "T67": ("Low", "partial"),
    "T69": ("Low", "fixed"),
    "T70": ("Low", "partial"),
    "T71": ("Low", "open"),
    "T72": ("Low", "fixed"),
    "T73": ("Low", "fixed"),
}
COLOR = {"fixed": FIXED, "partial": PARTIAL, "open": THREAT, "accepted": PARTIAL}

# One-line titles for the badged IDs, in legend order.
BADGED = {
    "T11": "Parsers in a credential-free container; egress shared",
    "T31": "Per-user access by default: each user reaches their own jobs",
    "T34": "POST /api/models removed; registry takes models bucket only",
    "T37": "Account-wide S3 BPA: opt-in, never deleted",
    "T38": "Add-ons have their IRSA roles; credentials checked",
    "T54": "PLY textures, E57 DTDs and format mismatch refused",
    "T60": "Job delete API; retention documented",
    "T12": "LAS and E57 allocations bounded by file size",
    "T13": "Stale-header anomalies are warnings, not outcomes",
    "T19": "Messages checked against job records; IAM per prefix",
    "T20": "Worker egress: TCP 443 to any internet host",
    "T28": "Pretrained checkpoints SHA-256 pinned, weights-only",
    "T35": "Enforcing Content-Security-Policy",
    "T41": "Fluent Bit runs on GPU nodes",
    "T43": "Request bounds; no per-caller quota",
    "T44": "Request bodies capped at 1 MiB",
    "T45": "No attributable audit trail",
    "T49": "Default-deny and egress rules on every pod",
    "T53": "Whole-load capped; in-process tile re-read remains",
    "T57": "HSTS sent; client https check open",
    "T59": "Worker logs are JSON records",
    "T66": "Failed send marks failed; crashes, deadline open",
    "T72": "Password-only sign-in unless MFA is required",
    "T16": "Unauthenticated API: now Cognito sign-in at the ALB",
    "T69": "CSRF / DNS rebinding: SPA header, host allowlist",
    "T70": "Users' email addresses held in Cognito",
    "T71": "Ingress or Service write can take sign-in off a rule",
    "T73": "Signed ALB claims verified: signer, client, pool, expiry",
}
SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}

c = Canvas(1200, 1330)


def zone(x1, y1, x2, y2, label, where="top-left"):
    c.box(
        x1, y1, x2, y2, label, stroke=THREAT, dash="8 5", width=2, where=where, size=14
    )


def badge(x, y, tid):
    _, status = REGISTER[tid]
    c.badge(x, y, tid, COLOR[status], outline=status == "accepted", pill=True)


def threat(x, y, *ids, step=46):
    for i, tid in enumerate(ids):
        badge(x + i * step, y, tid)


def flow(x, y, n):
    c.badge(x, y, n, DATA)


# ------------------------------------------------------------ boundaries ----
zone(15, 70, 205, 520, "Internet · untrusted", where="bottom-left")
zone(15, 550, 205, 775, "Third-party host")
zone(225, 70, 415, 775, "Edge · public subnets", where="bottom-left")
zone(430, 70, 835, 775, "EKS cluster · private subnets")
c.box(
    445,
    300,
    815,
    758,
    "Worker pod · IRSA in worker only · no ingress · egress DNS + 443",
    stroke="#8c4fff",
    fill="#f7f3ff",
    size=14,
)
zone(462, 432, 800, 580, "Parser container · no credentials", where="bottom-left")
zone(860, 70, 1185, 775, "AWS services · IAM-authorized, TLS", where="bottom-left")

# ----------------------------------------------------------------- nodes ----
c.node(110, 170, "aws/general/user", "Customer (browser)\nALB session cookie", size=54)
c.node(110, 360, "aws/general/users", "Client without\na session", size=54)
c.node(
    110, 665, "aws/general/internet-alt1", "Public model host\npretrained .pth", size=50
)

c.node(
    320,
    250,
    "aws/network/elb-application-load-balancer",
    "ALB · HTTPS :443\nCognito sign-in, every rule\n/api: SPA header or 403",
    size=54,
    font=13,
)

c.node(
    505,
    120,
    "programming/framework/react",
    "Frontend pod · nginx",
    where="right",
    size=40,
)
c.node(795, 108, "k8s/clusterconfig/hpa", "Add-ons", size=32, font=13)
c.node(530, 240, "programming/framework/fastapi", "API pod · per-user scope", size=48)
c.node(
    760, 370, "k8s/compute/pod", "Job processor\nSQS consumer", where="left", size=48
)
c.node(
    530,
    506,
    "aws/general/generic-sdk",
    "Parser (uid 1001)\nlaspy · pye57 · trimesh",
    where="right",
    size=46,
)
c.node(
    740,
    682,
    "aws/storage/elastic-block-store-ebs-volume",
    "Scratch volume\nemptyDir, capped",
    where="left",
    size=46,
)
c.node(
    520, 682, "aws/general/toolkit", "Checkpoint\nloader (torch.load)", size=44, dy=-2
)

bucket = "aws/storage/simple-storage-service-s3-bucket"
right = [
    (110, "aws/security/cognito", "Amazon Cognito\ninvite-only user pool"),
    (
        205,
        "aws/integration/simple-queue-service-sqs-queue",
        "SQS job queue\njob + config",
    ),
    (290, "aws/database/dynamodb-table", "DynamoDB\njob records"),
    (388, bucket, "S3 output + tiles\nprefix-scoped writes"),
    (475, "aws/management/cloudwatch", "CloudWatch Logs\nvia Fluent Bit"),
    (575, bucket, "S3 models bucket\nregistered weights"),
    (682, bucket, "S3 input bucket\nuploads/* only"),
]
for y, icon, label in right:
    c.node(930, y, icon, label, where="right", size=44)

# ----------------------------------------------------------------- flows ----
# 4: upload straight to S3 over presigned part URLs, routed over the top.
c.edge([(90, 143), (90, 22), (1178, 22), (1178, 635), (930, 635), (930, 658)])
flow(640, 22, "4")
c.text(
    658,
    17,
    "presigned per-part PUT, uploads/* only",
    size=13,
    color=DATA,
    anchor="start",
)
# 36: sign-in at Cognito's managed login; 37: the ALB redeems the code.
c.edge([(116, 143), (116, 40), (930, 40), (930, 88)], tail=True)
flow(250, 40, "36")
c.text(
    268,
    35,
    "sign in: email, password, TOTP",
    size=13,
    color=DATA,
    anchor="start",
)
c.edge([(320, 222), (320, 57), (895, 57), (895, 110), (908, 110)])
flow(560, 57, "37")
c.text(578, 52, "code exchange, session checks", size=12, color=DATA, anchor="start")

# 1-3: browser to the ALB, then UI and API; a request without a session is refused.
c.edge([(137, 170), (262, 170), (262, 240), (292, 240)])
flow(200, 170, "1")
c.edge([(137, 360), (240, 360), (240, 262), (292, 262)], color=THREAT, dash="6 4")
c.text(186, 352, "401 or sign-in", size=13, color=THREAT, bg=True)
c.edge([(348, 240), (390, 240), (390, 120), (484, 120)])
flow(390, 185, "2")
c.edge([(348, 256), (505, 256)])
flow(440, 256, "3")

# 5-6: API enqueues the job and writes its record.
c.edge([(554, 222), (570, 222), (570, 165), (930, 165), (930, 183)])
flow(730, 165, "5")
c.edge([(554, 250), (875, 250), (875, 284), (908, 284)])
flow(700, 250, "6")

# 8, 10, 11: worker receives, writes results and tiles, writes its own status.
c.edge([(908, 205), (842, 205), (842, 352), (786, 352)])
flow(842, 228, "8")
c.edge([(786, 370), (856, 370), (856, 296), (908, 296)])
flow(856, 345, "11")
c.edge([(786, 388), (908, 388)])
flow(888, 388, "10")

# The worker hands untrusted bytes to the parser container through the spool: T11.
c.edge([(760, 396), (760, 420), (530, 420), (530, 480)], color=THREAT)

# 9: scan into the scratch volume; the parsers read it and write tile extracts.
c.edge([(908, 690), (766, 690)])
flow(790, 690, "9")
c.edge([(740, 656), (740, 540), (556, 540)], tail=True)
c.text(746, 606, "extracts", size=12, color=DATA, anchor="start")

# Checkpoints: registered ones from S3; pretrained ones from the public host (12).
c.edge([(908, 580), (852, 580), (852, 612), (520, 612), (520, 658)])
c.edge([(137, 665), (496, 665)])
flow(170, 665, "12")
c.text(330, 657, "HTTPS, opt-in, SHA-256 pinned", size=13, color=DATA, bg=True)

# Anything else the worker reaches for: NetworkPolicy allows TCP 443 only.
c.edge([(445, 745), (205, 745)], color=PLATFORM, dash="6 4")
c.text(322, 738, "other egress: TCP 443 only", size=13, color=PLATFORM, bg=True)

# ---------------------------------------------------------------- badges ----
threat(275, 355, "T16", "T69", "T57")
threat(275, 383, "T71", "T45")
threat(598, 222 + 6, "T31", "T73", "T34", "T44", "T43", "T66")
threat(578, 150, "T35")
threat(752, 108, "T38")
threat(1150, 98, "T72")
threat(1150, 126, "T70")
threat(478, 340, "T49")
threat(478, 368, "T59")
threat(594, 458, "T11", "T54", "T12", "T13", "T53", step=44)
threat(250, 718, "T20")
threat(1150, 388, "T19")
threat(1150, 475, "T41")
threat(470, 700, "T28")
threat(1150, 670, "T37")
threat(1150, 698, "T60")

# ---------------------------------------------------------------- legend ----
y = 812
c.text(
    15,
    y,
    "Threats per docs/security/THREAT_MODEL.md",
    size=15,
    weight="bold",
    anchor="start",
)
key = [
    ("fixed", "Fixed"),
    ("partial", "Partially fixed"),
    ("open", "Open"),
    ("accepted", "Accepted (3.3)"),
]
x = 372
for status, label in key:
    c.badge(x, y - 5, "T", COLOR[status], outline=status == "accepted")
    c.text(x + 18, y, label, size=14, anchor="start")
    x += 42 + len(label) * 7.6
flow(x + 8, y - 5, "1")
c.text(x + 26, y, "flow ID", size=14, anchor="start")

rows = sorted(BADGED, key=lambda t: (SEVERITY_ORDER[REGISTER[t][0]], int(t[1:])))
half = (len(rows) + 1) // 2
for i, tid in enumerate(rows):
    col, row = divmod(i, half)
    bx = 32 + col * 600
    by = y + 34 + row * 24
    badge(bx, by - 5, tid)
    c.text(bx + 27, by, REGISTER[tid][0], size=13, anchor="start", color=PLATFORM)
    c.text(bx + 80, by, BADGED[tid], size=14, anchor="start")

y2 = y + 34 + half * 24 + 8
notes = [
    "Also in the register, Low, on drawn components: ALB and pods T46 · api T55 T58 · "
    "SPA T56 · worker T14 T21 · pods T48 · add-ons T67 · logs T36 T39 T40 · "
    "IAM and KMS T63 · model T65",
    "Fixed, Low: T5 T15 T17 T18 T22 T23 T24 T25 T26 T27. Not drawn (operator, build, "
    "supply chain, local modes): T29 T30 T32 T33 T42 T47 T50 T51 T52 T61 T62 T64",
]
for i, note in enumerate(notes):
    c.text(15, y2 + i * 20, note, size=13, anchor="start")

# Port-forward mode inset.
iy = y2 + 50
c.box(15, iy, 1185, iy + 60, "", stroke=PLATFORM, dash="5 4", fill="#fafafa")
c.text(
    28,
    iy + 23,
    "Port-forward mode (no domain):",
    size=14,
    weight="bold",
    anchor="start",
)
c.text(
    252,
    iy + 23,
    "no ALB and no Cognito. Users and scripts run kubectl port-forward through the EKS "
    "API, authorised by IAM,",
    size=14,
    anchor="start",
)
c.text(
    28,
    iy + 45,
    "an EKS access entry and a namespaced Role; access is shared. nginx proxies /api "
    "only with the SPA header and a localhost Host (T31, T49, T69).",
    size=14,
    anchor="start",
)

c.save(OUT)
