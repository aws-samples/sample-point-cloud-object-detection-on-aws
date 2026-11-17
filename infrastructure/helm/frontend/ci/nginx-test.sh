#!/bin/sh
# Run `nginx -t` on rendered server blocks, inside the image the frontend runs on
# (nginxinc/nginx-unprivileged:alpine). Each file is tested alone, as the pod's only
# conf.d file.
#
# The resolver name is cluster DNS, which does not exist in CI, and nginx resolves it
# while parsing. It is swapped for a literal address here; everything else is tested
# as rendered.
set -eu

mkdir -p /tmp/nginx-ci/conf.d
sed 's#/etc/nginx/conf.d/\*\.conf#/tmp/nginx-ci/conf.d/*.conf#' /etc/nginx/nginx.conf > /tmp/nginx-ci/nginx.conf

for conf in "$@"; do
  echo "== nginx -t: ${conf}"
  sed 's/resolver kube-dns\.kube-system\.svc\.cluster\.local /resolver 127.0.0.1 /' "${conf}" \
    > /tmp/nginx-ci/conf.d/default.conf
  nginx -t -c /tmp/nginx-ci/nginx.conf
done
