{{/*
The container image, repository:tag: the GPU image (Dockerfile.gpu), which has the
training stack. Both are required, and the tag may not be "latest": pullPolicy is
IfNotPresent, so a node that already holds a mutable tag never pulls it again, and
which build runs would depend on the node the pod lands on (THREAT_MODEL.md T29).
*/}}
{{- define "pcd.image" -}}
{{- $repo := required "image.repository is required: the ECR repository holding the GPU image built from Dockerfile.gpu (the backend repository in terraform output ecr_repository_urls)." .Values.image.repository }}
{{- $tag := required "image.tag is required: the immutable tag of the GPU image, such as <git-sha>-gpu. There is no default, because a default can only be a mutable tag." .Values.image.tag | toString }}
{{- if eq $tag "latest" }}
{{- fail "image.tag=latest is refused. pullPolicy is IfNotPresent, so a node that already has a latest image keeps running it, and the ECR repositories are IMMUTABLE, so a second latest push is rejected. Pass the immutable tag of the GPU image." }}
{{- end }}
{{- printf "%s:%s" $repo $tag }}
{{- end }}

{{/*
The NetworkPolicy guards, as in the backend chart: enabled=false needs an explicit
acknowledgement (T61), and vpcCidr is required, because without it the HTTPS egress
rule admits the whole VPC, the private EKS endpoint included (T49).
*/}}
{{- define "pcd.validateNetworkPolicy" -}}
{{- $np := .Values.networkPolicy }}
{{- if not $np.enabled }}
{{- if not $np.acknowledgeUnprotectedApi }}
{{- fail "networkPolicy.enabled=false renders no NetworkPolicy for the training pods, which could then reach the API, which authenticates nothing itself, and any in-VPC listener. Keep it enabled, or set networkPolicy.acknowledgeUnprotectedApi=true to confirm that another control does this job." }}
{{- end }}
{{- else if not $np.vpcCidr }}
{{- fail "networkPolicy.vpcCidr is required: training may send HTTPS anywhere except the VPC, and without its CIDR (terraform output vpc_cidr) that includes the private EKS endpoint and every in-VPC listener." }}
{{- end }}
{{- end }}
