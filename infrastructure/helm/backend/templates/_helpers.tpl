{{/*
The container image, repository:tag. Both are required, and the tag may not be
"latest": pullPolicy is IfNotPresent, so a node that already holds a mutable tag never
pulls it again, and which build runs would depend on the node a pod lands on. deploy.sh
passes the git-SHA tag build-push.sh pushed (THREAT_MODEL.md T29).
*/}}
{{- define "pcd.image" -}}
{{- $repo := required "image.repository is required: the backend ECR repository URL (terraform output ecr_repository_urls). deploy.sh passes it." .Values.image.repository }}
{{- $tag := required "image.tag is required: the immutable tag build-push.sh pushed, such as the git SHA. deploy.sh passes it; there is no default, because a default can only be a mutable tag." .Values.image.tag | toString }}
{{- if eq $tag "latest" }}
{{- fail "image.tag=latest is refused. pullPolicy is IfNotPresent, so a node that already has a latest image keeps running it, and the ECR repositories are IMMUTABLE, so a second latest push is rejected. Pass the git-SHA tag build-push.sh pushed." }}
{{- end }}
{{- printf "%s:%s" $repo $tag }}
{{- end }}

{{/*
Environment every backend container needs, because its root filesystem is read-only.

HOME is the container's /tmp emptyDir: the image user's home is on the read-only root,
and libraries cache under it (in the GPU image, torch hub keeps downloaded checkpoints
there).

PYTHONPATH: the image makes its editable install of the project before it copies src/,
so the venv has no path entry for the package, and `uv run` used to repair that on
every start by writing into the venv. With the package on the path the venv's own
interpreter needs no write. Redundant, and harmless, once the image installs it.
*/}}
{{- define "pcd.runtimeEnv" -}}
- name: HOME
  value: /tmp
- name: PYTHONPATH
  value: /app/src
{{- end }}

{{/*
NetworkPolicy guards, shared by the three charts in spirit (each has its own copy).

- networkPolicy.enabled=false renders no policy at all, which, since the API has no
  authentication of its own, opens every route to every pod in the cluster (T61). It is
  refused unless networkPolicy.acknowledgeUnprotectedApi=true says so on purpose.
- networkPolicy.vpcCidr is what keeps HTTPS egress off in-VPC listeners, such as the
  private EKS endpoint. Empty, the 443 rules would admit the whole VPC (T49).
*/}}
{{- define "pcd.validateNetworkPolicy" -}}
{{- $np := .Values.networkPolicy }}
{{- if not $np.enabled }}
{{- if not $np.acknowledgeUnprotectedApi }}
{{- fail "networkPolicy.enabled=false renders no NetworkPolicy, and the API authenticates nothing itself, so every pod in the cluster could call every route and the worker could reach any in-VPC listener. Keep it enabled, or set networkPolicy.acknowledgeUnprotectedApi=true to confirm that another control does this job." }}
{{- end }}
{{- else if not $np.vpcCidr }}
{{- fail "networkPolicy.vpcCidr is required: the api and worker may send HTTPS anywhere except the VPC, and without its CIDR (terraform output vpc_cidr) that includes the private EKS endpoint and every in-VPC listener. deploy.sh passes it." }}
{{- end }}
{{- end }}

{{/*
Egress every backend pod needs: cluster DNS, and HTTPS to anywhere outside the VPC and
the link-local range (S3, SQS, DynamoDB, STS; the checkpoint host for the worker). Not
the cluster API, other pods, node services or instance metadata. IRSA credentials come
from STS, not IMDS, so blocking 169.254.0.0/16 costs nothing.
*/}}
{{- define "pcd.egressDnsAndHttps" -}}
- to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: kube-system
      podSelector:
        matchLabels:
          k8s-app: kube-dns
  ports:
    - port: 53
      protocol: UDP
    - port: 53
      protocol: TCP
- to:
    - ipBlock:
        cidr: 0.0.0.0/0
        except:
          - {{ .Values.networkPolicy.vpcCidr }}
          - 169.254.0.0/16
  ports:
    - port: 443
      protocol: TCP
{{- end }}
