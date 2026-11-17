output "instance_id" {
  description = "Bastion (SSM relay) instance ID"
  value       = aws_instance.bastion.id
}

output "role_arn" {
  description = "Bastion IAM role ARN"
  value       = aws_iam_role.bastion.arn
}

output "cluster_endpoint_host" {
  description = "Host the relay forwards to: the EKS API endpoint, which resolves to its private addresses inside the VPC"
  value       = local.cluster_endpoint_host
}

# The API server's certificate names the endpoint host, not 127.0.0.1, so the kubeconfig
# keeps the host as its TLS server name while connecting to the local end of the relay.
# A separate kubeconfig file keeps a later `aws eks update-kubeconfig` for the same
# cluster from resetting the server address in the default one.
output "relay_commands" {
  description = "Commands to reach the private EKS API through the relay with your own IAM identity"
  value       = <<-EOT
    # 1. Open the relay and leave it running (needs the Session Manager plugin for the AWS CLI):
    aws ssm start-session --region ${local.region} --target ${aws_instance.bastion.id} --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters '{"host":["${local.cluster_endpoint_host}"],"portNumber":["443"],"localPortNumber":["${local.relay_local_port}"]}'

    # 2. In another shell, write a kubeconfig that goes through it, then use kubectl and helm as usual:
    aws eks update-kubeconfig --region ${local.region} --name ${var.cluster_name} --kubeconfig ~/.kube/${var.cluster_name}-relay
    kubectl --kubeconfig ~/.kube/${var.cluster_name}-relay config set-cluster ${var.cluster_arn} --server https://127.0.0.1:${local.relay_local_port} --tls-server-name ${local.cluster_endpoint_host}
    export KUBECONFIG=~/.kube/${var.cluster_name}-relay

    # deploy.sh and destroy.sh write kubeconfigs of their own; with the relay from step 1 running, give them its port:
    PCD_KUBE_RELAY_PORT=${local.relay_local_port} infrastructure/scripts/deploy.sh
  EOT
}

output "operator_policy_json" {
  description = "IAM policy for the operators who use the relay: ssm:StartSession on this instance with the port-forwarding document only, not a shell"
  value       = jsonencode(local.operator_policy)
}
