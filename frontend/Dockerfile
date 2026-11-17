# Multi-stage build for React frontend
#
# Both images are pinned by digest and pulled from their ECR Public mirrors: the Docker
# Official Image for node, and NGINX's own repository for nginx-unprivileged. Each
# serves the same digest as Docker Hub. Dependabot (docker ecosystem) refreshes them.
# The nginx reference is repeated in the `nginx -t` steps of both CI files, which must
# test the image this one ships; change all three together.
#
# checkov:skip=CKV_DOCKER_2: No HEALTHCHECK. Kubernetes ignores it; the Helm chart's
# liveness and readiness probes do this job.
FROM public.ecr.aws/docker/library/node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS builder

WORKDIR /app
# The lockfile is required, not optional: npm ci refuses to run without it.
# --ignore-scripts: no dependency gets to run install hooks in the builder. The native
# pieces the build needs (rolldown, esbuild) ship as per-platform optional packages,
# not postinstall downloads, so nothing is lost.
COPY package.json package-lock.json ./
# The npm bundled with the base image carries HIGH advisories in its own dependencies
# (brace-expansion, pacote, sigstore and others) that only a newer npm fixes. Pinned,
# and run with --ignore-scripts like the install below; 12.2.1 needs node ^22.22.2.
RUN npm install -g npm@12.2.1 --ignore-scripts && npm ci --ignore-scripts

COPY . .
RUN npm run build

# Production: serve with nginx. The unprivileged variant runs as uid 101 and keeps
# its pid and temp files under /tmp, so the container needs no root, no capabilities
# and no writable root filesystem. It cannot bind port 80 without root, hence 8080.
FROM public.ecr.aws/nginx/nginx-unprivileged:alpine@sha256:f90ef283a8a580912aa56315c3ed8f54cf50c2e89ed7e6fbf4c53368b55d712f

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

USER 101
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
