variable "prefix" {
  description = "Prefix for bastion resource names"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID for the bastion security group"
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR. The bastion's only egress is TCP 443 to this range: the SSM interface endpoints and the EKS API endpoint."
  type        = string

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0)) && var.vpc_cidr != "0.0.0.0/0"
    error_message = "vpc_cidr must be the VPC's CIDR block, not 0.0.0.0/0."
  }
}

variable "private_subnet_id" {
  description = "Private subnet ID to place the bastion, and its SSM interface endpoints, in (no public IP)"
  type        = string
}

variable "cluster_name" {
  description = "EKS cluster name (for the relay_commands output)"
  type        = string
}

variable "cluster_arn" {
  description = "EKS cluster ARN; aws eks update-kubeconfig names the kubeconfig cluster entry after it (relay_commands output)"
  type        = string
}

variable "cluster_endpoint" {
  description = "EKS API endpoint URL (https://<host>). The relay forwards to its host on 443."
  type        = string
}

variable "cluster_security_group_id" {
  description = "EKS cluster security group ID to allow the bastion to reach the API"
  type        = string
}

variable "instance_type" {
  description = "Bastion instance type"
  type        = string
  default     = "t3.small"
}
