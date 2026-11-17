variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
}

variable "vpc_id" {
  description = "ID of the cluster VPC. The Load Balancer Controller may change security-group ingress only in this VPC."
  type        = string
}

variable "oidc_provider_arn" {
  description = "EKS OIDC provider ARN for IRSA trust"
  type        = string
}

variable "oidc_issuer" {
  description = "EKS OIDC issuer URL without the https:// prefix"
  type        = string
}

variable "sqs_queue_arn" {
  description = "ARN of the detection job queue. KEDA reads its depth to scale the worker Deployment, so the IRSA policy is scoped to exactly this queue."
  type        = string
}

# KEDA installs into its own namespace rather than kube-system (the chart default),
# so the IRSA trust condition cannot reuse the kube-system assumption the other
# addons make. These MUST match the `helm upgrade --install keda` invocation in
# scripts/deploy.sh: the trust policy is scoped to this exact namespace/SA pair,
# and a mismatch fails at runtime as an opaque AccessDenied from the scaler, not
# at apply time.
variable "keda_namespace" {
  description = "Namespace KEDA is installed into."
  type        = string
  default     = "keda"
}

variable "keda_service_account" {
  description = "KEDA operator service account name. Only the operator SA needs AWS credentials; the metrics server reaches the scaler over gRPC and holds none."
  type        = string
  default     = "keda-operator"
}
