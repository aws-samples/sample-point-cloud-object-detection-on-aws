# Architecture Diagrams

All diagrams use [Mermaid](https://mermaid.js.org/) for source-controlled, renderable diagrams. GitHub/GitLab render these inline in markdown.

## Diagrams

| # | Diagram | File | Used In |
|---|---------|------|---------|
| 1 | [Solution Architecture](solution-architecture.md) | `solution-architecture.md` | Blog 1, Solution Guidance overview, README |
| 2 | [Data Flow](data-flow.md) | `data-flow.md` | Blog 2, Solution Guidance architecture |
| 3 | [EKS Cluster Topology](eks-cluster-topology.md) | `eks-cluster-topology.md` | Solution Guidance deployment guide |
| 4 | [Fine-Tuning Workflow](fine-tuning-workflow.md) | `fine-tuning-workflow.md` | Blog 2, Workshop step 6 |
| 5 | [User Workflow](user-workflow.md) | `user-workflow.md` | Blog 1, Blog 2 |
| 6 | Threat model data flow | `src/threat_dataflow.py` | `docs/security/THREAT_MODEL.md`, security review |

### Editable draw.io source

`user-workflow.drawio` is the editable source for diagram 5, kept alongside the
Mermaid version for authors who prefer visual editing. It is stored as
uncompressed XML so diffs stay readable. Open it at
[app.diagrams.net](https://app.diagrams.net) or with the VS Code "Draw.io
Integration" extension. If you change one, update the other so they do not drift.

### Rendered exports

`exports/` holds rendered assets for publication. These are committed rather than
generated at build time so blog and guidance authors can drop them straight in.

| Asset | Source | Size |
|-------|--------|------|
| `exports/user-workflow.png` | `user-workflow.drawio` | 2857 x 1594 (2x) |
| `exports/architecture-v2.png` / `.svg` | `src/architecture_v2.py` | 2400 x 2124 (2x) |
| `exports/architecture-v2-blog.png` / `.svg` | `src/architecture_v2_blog.py` | 1600 x 1480 (2x of an 800-wide canvas) |
| `exports/threat-dataflow.png` / `.svg` | `src/threat_dataflow.py` | 2400 x 2660 (2x) |

### AWS-icon diagrams

`architecture-v2` (repository and docs figure), `architecture-v2-blog` (blog
Figure 1) and `threat-dataflow` (trust boundaries and threat
IDs for the security review) use the official AWS Architecture Icons, vendored by the
[`diagrams`](https://diagrams.mingrammer.com/) package. They are laid out by hand on
a fixed canvas in `src/` rather than auto-laid-out, because Graphviz produced crossing
edges and overlapping labels at this density. Each element is traceable to Terraform,
the Helm charts, or `deploy.sh`, so update the script when those change.

All three show the default public access mode: an HTTPS ALB that signs users in with
Amazon Cognito on every rule. Port-forward mode, which deploys no ALB and no Cognito,
is a footnote on the architecture figures and an inset on the threat diagram.

`threat-dataflow` mirrors the threat register in `docs/security/THREAT_MODEL.md`: the
`REGISTER` table in its script holds each ID's severity and status, and the badges are
coloured from it. High and Medium threats on a drawn component are badged, as are the
Low ones that describe sign-in; every other ID is listed in the legend. Its blue numbers
are that document's flow IDs. When the register changes, change `REGISTER` in the same
commit.

```bash
uv run --no-project --with diagrams==0.25.1 --with resvg-py==0.5.0 python docs/diagrams/src/architecture_v2.py
uv run --no-project --with diagrams==0.25.1 --with resvg-py==0.5.0 python docs/diagrams/src/architecture_v2_blog.py
uv run --no-project --with diagrams==0.25.1 --with resvg-py==0.5.0 python docs/diagrams/src/threat_dataflow.py
```

The blog cut is drawn on an 800-unit canvas, the AWS blog's maximum image width, so
its font sizes are what a reader sees (13 or larger). It condenses the full figure:
one S3 node for the four buckets, and security and operations services as a footer
line. Keep the two in step.

PNG export renders in-process with `resvg-py`, so it needs no system library.

Regenerate the draw.io export after editing its source:

```bash
/Applications/draw.io.app/Contents/MacOS/draw.io --export --format png \
  --scale 2 --border 24 \
  --output docs/diagrams/exports/user-workflow.png \
  docs/diagrams/user-workflow.drawio
```

The draw.io version of diagram 5 is the one sized for the blog: type is set so
labels stay legible when the image is scaled down to a ~1000px content column,
and long labels are deliberately short, with the detail carried by the prose in
`user-workflow.md` rather than crammed into boxes. The Mermaid version is the
repo-facing one that renders inline on GitLab and GitHub.

## Rendering

- **GitLab/GitHub**: Mermaid blocks render automatically in markdown preview
- **Local**: Use [Mermaid CLI](https://github.com/mermaid-js/mermaid-cli) to export PNG/SVG:
  ```bash
  npx @mermaid-js/mermaid-cli@12.0.0 -i solution-architecture.md -o solution-architecture.png
  ```
- **VS Code**: Install "Markdown Preview Mermaid Support" extension

## Exporting for Publication

For the blog and solution guidance docs, export at 2x resolution:
```bash
npx @mermaid-js/mermaid-cli@12.0.0 -i solution-architecture.md -o solution-architecture.png -s 2
```
