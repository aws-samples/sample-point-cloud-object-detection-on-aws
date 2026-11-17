{{/*
The container image, repository:tag. Both are required, and the tag may not be
"latest": pullPolicy is IfNotPresent, so a node that already holds a mutable tag never
pulls it again, and which build runs would depend on the node a pod lands on. deploy.sh
passes the git-SHA tag build-push.sh pushed (THREAT_MODEL.md T29).
*/}}
{{- define "pcd.image" -}}
{{- $repo := required "image.repository is required: the frontend ECR repository URL (terraform output ecr_repository_urls). deploy.sh passes it." .Values.image.repository }}
{{- $tag := required "image.tag is required: the immutable tag build-push.sh pushed, such as the git SHA. deploy.sh passes it; there is no default, because a default can only be a mutable tag." .Values.image.tag | toString }}
{{- if eq $tag "latest" }}
{{- fail "image.tag=latest is refused. pullPolicy is IfNotPresent, so a node that already has a latest image keeps running it, and the ECR repositories are IMMUTABLE, so a second latest push is rejected. Pass the git-SHA tag build-push.sh pushed." }}
{{- end }}
{{- printf "%s:%s" $repo $tag }}
{{- end }}

{{/*
The header every /api request must carry in public mode. Fixed here rather than a
value: the SPA sends exactly this pair (frontend/src/services/http.ts), so making it
configurable could only make the two disagree. The name must match [-A-Za-z0-9]+,
because the ALB drops invalid header fields before rules see them.
*/}}
{{- define "pcd.clientHeaderName" -}}X-PCD-Client{{- end }}
{{- define "pcd.clientHeaderValue" -}}spa{{- end }}

{{/*
Refuse to render a configuration that could put an unauthenticated listener on the
internet. Called from ingress.yaml, which renders in every mode.
*/}}
{{- define "pcd.validate" -}}
{{- if not (has .Values.accessMode (list "public" "port-forward")) }}
{{- fail (printf "accessMode=%q is not valid. Use public (internet-facing ALB with Cognito sign-in) or port-forward (no load balancer)." .Values.accessMode) }}
{{- end }}
{{- if eq .Values.accessMode "public" }}
{{- $i := .Values.ingress }}
{{- if ne $i.className "alb" }}
{{- fail (printf "ingress.className=%q: the sign-in is Load Balancer Controller annotations, which any other ingress controller ignores, so it would serve these Ingresses with no authentication. Only the AWS Load Balancer Controller's alb class is supported." $i.className) }}
{{- end }}
{{- if not $i.certificateArn }}
{{- fail "accessMode=public requires ingress.certificateArn. The ALB authenticates only on HTTPS listeners, so without a certificate every request would reach the pods unauthenticated. For a cluster with no domain, use accessMode=port-forward." }}
{{- end }}
{{- if not $i.host }}
{{- fail "accessMode=public requires ingress.host, the hostname on the certificate. It is also the only Cognito callback URL, so sign-in cannot work without it." }}
{{- end }}
{{- if not (and $i.auth.userPoolArn $i.auth.userPoolClientId $i.auth.userPoolDomain) }}
{{- fail "accessMode=public requires ingress.auth.userPoolArn, userPoolClientId and userPoolDomain: an internet-facing ALB is only rendered with Cognito sign-in on every rule. deploy.sh passes them from `terraform output -json cognito`." }}
{{- end }}
{{- if not (regexMatch "^arn:aws[a-z-]*:cognito-idp:[a-z0-9-]+:[0-9]{12}:userpool/[A-Za-z0-9_-]+$" $i.auth.userPoolArn) }}
{{- fail (printf "ingress.auth.userPoolArn=%q is not a Cognito user pool ARN." $i.auth.userPoolArn) }}
{{- end }}
{{- if not (regexMatch "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$" $i.auth.userPoolDomain) }}
{{- fail (printf "ingress.auth.userPoolDomain=%q must be the domain prefix only (e.g. my-prefix), not a URL or full hostname." $i.auth.userPoolDomain) }}
{{- end }}
{{- if and .Values.networkPolicy.enabled (not .Values.networkPolicy.albSourceCidrs) }}
{{- fail "networkPolicy.albSourceCidrs is required in public mode. The ALB targets frontend pod IPs directly, so without its source CIDRs (terraform output public_subnet_cidrs) the policy blocks the UI." }}
{{- end }}
{{- /* security-groups replaces the controller's own group, which silently drops
inbound-cidrs, and a group without outbound 443 breaks every sign-in: the ALB calls
Cognito's token endpoint itself. */}}
{{- $locked := list "scheme" "listen-ports" "ssl-redirect" "certificate-arn" "ssl-policy" "inbound-cidrs" "load-balancer-attributes" "target-type" "healthcheck-path" "security-groups" "manage-backend-security-group-rules" }}
{{- range $k, $_ := $i.annotations }}
{{- $name := trimPrefix "alb.ingress.kubernetes.io/" $k }}
{{- if and (hasPrefix "alb.ingress.kubernetes.io/" $k) (or (has $name $locked) (hasPrefix "auth-" $name) (hasPrefix "group." $name) (hasPrefix "conditions." $name) (hasPrefix "actions." $name) (hasPrefix "listener-attributes." $name)) }}
{{- fail (printf "ingress.annotations may not set %s. The chart owns the listeners and their attributes (HSTS), sign-in, routing, grouping and security groups, and overriding any of them can leave a rule unauthenticated or drop the inbound allowlist. Use ingress.certificateArn, ingress.inboundCidrs or ingress.extraLoadBalancerAttributes instead, or change the template deliberately." $k) }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Annotations both Ingresses carry. Several are "exclusive" in an IngressGroup (scheme,
ssl-redirect, ssl-policy, load-balancer-attributes, inbound-cidrs): every member must
agree, so they come from this one definition. certificate-arn and listen-ports merge
across the group, but each Ingress must still set them itself: one without them gets
HTTP:80 only, and under ssl-redirect its rules are silently dropped. The auth
annotations must also be identical, so the session cookie issued on / is accepted on
/api.
*/}}
{{- define "pcd.ingressAnnotations" -}}
{{- $i := .Values.ingress }}
{{- with $i.annotations }}
{{ toYaml . }}
{{- end }}
alb.ingress.kubernetes.io/group.name: {{ $i.groupName | quote }}
alb.ingress.kubernetes.io/scheme: internet-facing
alb.ingress.kubernetes.io/target-type: ip
# Drop malformed headers at the ALB rather than forwarding them: the standard
# mitigation for HTTP desync / request smuggling against the backend.
alb.ingress.kubernetes.io/load-balancer-attributes: {{ join "," (compact (list "routing.http.drop_invalid_header_fields.enabled=true" $i.extraLoadBalancerAttributes)) | quote }}
{{- if $i.inboundCidrs }}
alb.ingress.kubernetes.io/inbound-cidrs: {{ $i.inboundCidrs | quote }}
{{- end }}
alb.ingress.kubernetes.io/certificate-arn: {{ $i.certificateArn | quote }}
# HTTP:80 exists only to redirect. With ssl-redirect set, the controller builds no
# rules on it, so nothing is ever served over plain HTTP.
alb.ingress.kubernetes.io/listen-ports: '[{"HTTP": 80}, {"HTTPS": 443}]'
alb.ingress.kubernetes.io/ssl-redirect: "443"
alb.ingress.kubernetes.io/ssl-policy: {{ $i.sslPolicy | quote }}
# T57: HSTS on every HTTPS response, including /api, which the ALB forwards straight to
# the api pods, and its own fixed responses. The ALB replaces a target's copy of the
# header with this one. listener-attributes merge across the IngressGroup; both
# Ingresses carry the same value so neither one's rules can change it.
alb.ingress.kubernetes.io/listener-attributes.HTTPS-443: {{ printf "routing.http.response.strict_transport_security.header_value=%s" (include "pcd.hsts" .) | quote }}
alb.ingress.kubernetes.io/auth-type: cognito
alb.ingress.kubernetes.io/auth-idp-cognito: {{ dict "userPoolARN" $i.auth.userPoolArn "userPoolClientID" $i.auth.userPoolClientId "userPoolDomain" $i.auth.userPoolDomain | toJson | squote }}
alb.ingress.kubernetes.io/auth-scope: {{ $i.auth.scope | quote }}
alb.ingress.kubernetes.io/auth-session-cookie: {{ $i.auth.sessionCookie | quote }}
alb.ingress.kubernetes.io/auth-session-timeout: {{ $i.auth.sessionTimeout | toString | quote }}
{{- end }}

{{/* Cognito sign-out endpoint; the landing page is / on the configured host. */}}
{{- define "pcd.cognitoLogoutUrl" -}}
{{- $a := .Values.ingress.auth }}
{{- $region := index (splitList ":" $a.userPoolArn) 3 }}
{{- printf "https://%s.auth.%s.amazoncognito.com/logout?client_id=%s&logout_uri=%s" $a.userPoolDomain $region $a.userPoolClientId (printf "https://%s/" (lower .Values.ingress.host) | urlquery) }}
{{- end }}

{{/*
T61: networkPolicy.enabled=false renders no policy for the frontend pods, and in
port-forward mode their nginx relays /api to an API with no authentication of its own.
Refused unless networkPolicy.acknowledgeUnprotectedApi=true says so on purpose.
*/}}
{{- define "pcd.validateNetworkPolicy" -}}
{{- if and (not .Values.networkPolicy.enabled) (not .Values.networkPolicy.acknowledgeUnprotectedApi) }}
{{- fail "networkPolicy.enabled=false renders no NetworkPolicy for the frontend pods, whose nginx relays /api (in port-forward mode) to an API that authenticates nothing itself, so any pod in the cluster could use it. Keep it enabled, or set networkPolicy.acknowledgeUnprotectedApi=true to confirm that another control does this job." }}
{{- end }}
{{- end }}

{{/*
Content-Security-Policy (T35). The SPA is one origin: its scripts, styles and fonts are
the bundle's own files, and it calls only /api on the same origin and, for uploads, the
input bucket's presigned URLs. api/deps.py signs those with a regional s3v4 client
and the default virtual-hosted addressing, so the host is the bucket's regional name,
or its global name in us-east-1 (botocore's choice for that Region); both are allowed.
img-src and worker-src take blob: and data: for three.js textures and workers.
frame-ancestors 'none' refuses every frame, as X-Frame-Options DENY does for browsers
that ignore CSP.
*/}}
{{- define "pcd.csp" -}}
{{- $s3 := .Values.aws.s3 }}
{{- $bucket := required "aws.s3.inputBucket is required: the browser uploads straight to the input bucket on presigned URLs, and the Content-Security-Policy admits only that bucket. deploy.sh passes it (terraform output input_bucket_name)." $s3.inputBucket }}
{{- $region := required "aws.region is required: the input bucket's Region, for the Content-Security-Policy." .Values.aws.region }}
{{- $upload := printf "https://%s.s3.%s.amazonaws.com https://%s.s3.amazonaws.com" $bucket $region $bucket }}
{{- list "default-src 'self'" "script-src 'self'" "style-src 'self'" "img-src 'self' data: blob:" "worker-src 'self' blob:" (printf "connect-src 'self' %s" $upload) "object-src 'none'" "base-uri 'self'" "frame-ancestors 'none'" "form-action 'self'" | join "; " }}
{{- end }}

{{/*
The security headers, for the server block and every location that sets add_header of
its own: nginx inherits add_header from the enclosing level only when a location sets
none, so each of those repeats them.
*/}}
{{- define "pcd.securityHeaders" -}}
add_header X-Frame-Options "DENY" always;
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header {{ ternary "Content-Security-Policy-Report-Only" "Content-Security-Policy" .Values.contentSecurityPolicy.reportOnly }} "{{ include "pcd.csp" . }}" always;
{{- /* T57: HTTPS only, for a year, on this host and its subdomains. The ALB sends
it on every response too (the listener attribute in pcd.ingressAnnotations); this copy
covers the pages if that attribute is ever lost. Not in port-forward mode, which is
plain HTTP on localhost. */}}
{{- if eq .Values.accessMode "public" }}
add_header Strict-Transport-Security "{{ include "pcd.hsts" . }}" always;
{{- end }}
{{- end }}

{{- define "pcd.hsts" -}}max-age=31536000; includeSubDomains{{- end }}
