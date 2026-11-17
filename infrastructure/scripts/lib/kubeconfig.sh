# shellcheck shell=bash
# Sourced by deploy.sh and destroy.sh: writes a kubeconfig of the script's own for the
# stack's cluster, so the operator's own kubeconfig is never rewritten.
#
# The cluster has a private API endpoint only, which an operator's machine reaches
# through the SSM relay (terraform output -raw bastion_relay_commands). With PCD_KUBE_RELAY_PORT
# set to the relay's local port, the kubeconfig connects to https://127.0.0.1:<port>
# and keeps the endpoint's host as the TLS server name, which the API server's
# certificate names. Start the relay first and leave it running.

# write_kubeconfig <cluster name> <region> <kubeconfig file>
write_kubeconfig() {
  local cluster="$1" region="$2" file="$3" port="${PCD_KUBE_RELAY_PORT:-}" out arn endpoint
  aws eks update-kubeconfig --name "${cluster}" --region "${region}" \
    --kubeconfig "${file}" >/dev/null
  [ -n "${port}" ] || return 0
  if ! [[ "${port}" =~ ^[0-9]{1,5}$ ]] || [ "${port}" -lt 1 ] || [ "${port}" -gt 65535 ]; then
    echo "error: PCD_KUBE_RELAY_PORT='${port}' is not a port number." >&2
    return 1
  fi
  out="$(aws eks describe-cluster --name "${cluster}" --region "${region}" \
    --query 'cluster.[arn,endpoint]' --output text)"
  read -r arn endpoint <<<"${out}"
  case "${arn}:${endpoint}" in
    arn:*:https://*) ;;
    *)
      echo "error: could not read the ARN and endpoint of cluster ${cluster}." >&2
      return 1
      ;;
  esac
  kubectl --kubeconfig "${file}" config set-cluster "${arn}" \
    --server "https://127.0.0.1:${port}" \
    --tls-server-name "${endpoint#https://}" >/dev/null
  echo "kubectl reaches cluster ${cluster} through the relay at 127.0.0.1:${port}."
}

# Why the cluster could not be administered, for the error message after a failed
# `kubectl auth can-i`.
cluster_access_hint() {
  if [ -n "${PCD_KUBE_RELAY_PORT:-}" ]; then
    cat <<MSG
Check that the SSM relay is running and listening on 127.0.0.1:${PCD_KUBE_RELAY_PORT}
(step 1 of \`terraform -chdir=infrastructure output -raw bastion_relay_commands\`).
MSG
  else
    cat <<'MSG'
The cluster has a private API endpoint only. Start the SSM relay (step 1 of
`terraform -chdir=infrastructure output -raw bastion_relay_commands`) and run this
again with PCD_KUBE_RELAY_PORT set to its local port, 8443 by default.
MSG
  fi
}
