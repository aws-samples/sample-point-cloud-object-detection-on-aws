output "alb_controller_role_arn" {
  description = "IRSA role ARN for the AWS Load Balancer Controller service account"
  value       = aws_iam_role.lbc.arn
}

output "cluster_autoscaler_role_arn" {
  description = "IRSA role ARN for the Cluster Autoscaler service account"
  value       = aws_iam_role.ca.arn
}

output "fluent_bit_role_arn" {
  description = "IRSA role ARN for the Fluent Bit service account"
  value       = aws_iam_role.fb.arn
}

output "keda_operator_role_arn" {
  description = "IRSA role ARN for the KEDA operator service account (sqs:GetQueueAttributes on the job queue only)"
  value       = aws_iam_role.keda.arn
}

output "keda_namespace" {
  description = "Namespace the KEDA IRSA trust policy is scoped to. Install KEDA here or the scaler cannot assume the role."
  value       = var.keda_namespace
}
