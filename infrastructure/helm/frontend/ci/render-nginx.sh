#!/bin/sh
# Print the nginx server block the chart renders for one access mode, so CI can run
# `nginx -t` on it (ci/nginx-test.sh). Needs only helm, sed and awk.
#
#   sh infrastructure/helm/frontend/ci/render-nginx.sh public > nginx-public.conf
set -eu

MODE="$1"
CHART="$(cd "$(dirname "$0")/.." && pwd)"

helm template ci "${CHART}" -f "${CHART}/ci/${MODE}-values.yaml" \
  --show-only templates/configmap-nginx.yaml |
  awk '
    found && /^    / { sub(/^    /, ""); print; next }
    found && /^$/     { print; next }
    found             { exit }
    /^  default.conf: \|/ { found = 1 }
  '
