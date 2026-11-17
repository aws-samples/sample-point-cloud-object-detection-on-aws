# Plan-only checks of the add-on IRSA policies. The AWS provider is mocked, so this
# needs no credentials and creates nothing:
#
#   terraform -chdir=infrastructure/modules/addons init -backend=false
#   terraform -chdir=infrastructure/modules/addons test

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = {
      region = "us-east-1"
    }
  }
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }
  # The mocked document is not rendered from its statements, so give it a value the
  # provider's JSON validation accepts. The assertions read the statements instead.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  cluster_name      = "pointcloud-detection-dev"
  vpc_id            = "vpc-0123456789abcdef0"
  oidc_provider_arn = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
  oidc_issuer       = "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
  sqs_queue_arn     = "arn:aws:sqs:us-east-1:111122223333:pointcloud-detection-dev-jobs"
}

run "cluster_autoscaler_policy_is_scoped" {
  command = plan

  assert {
    # Only actions IAM authorizes on "*" alone may stay on "*".
    condition = alltrue([
      for s in data.aws_iam_policy_document.ca.statement : alltrue([
        for a in s.actions : startswith(a, "autoscaling:Describe") || startswith(a, "ec2:Describe") || a == "ec2:GetInstanceTypesFromInstanceRequirements"
      ]) if contains(s.resources, "*")
    ])
    error_message = "a restrictable Cluster Autoscaler action is granted on \"*\""
  }
  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.ca.statement :
      s.actions == toset(["eks:DescribeNodegroup"]) && s.resources == toset(["arn:aws:eks:us-east-1:${data.aws_caller_identity.current.account_id}:nodegroup/pointcloud-detection-dev/*/*"])
    ])
    error_message = "eks:DescribeNodegroup must be scoped to this cluster's node groups"
  }
  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.ca.statement :
      contains(s.actions, "autoscaling:SetDesiredCapacity")
      && s.resources == toset(["arn:aws:autoscaling:us-east-1:${data.aws_caller_identity.current.account_id}:autoScalingGroup:*:autoScalingGroupName/*"])
      && anytrue([for c in s.condition : c.variable == "autoscaling:ResourceTag/k8s.io/cluster-autoscaler/pointcloud-detection-dev" && c.values == tolist(["owned"])])
    ])
    error_message = "ASG mutations must be scoped to autoScalingGroup ARNs and keep the owned-tag condition"
  }
}

run "cluster_autoscaler_trust_is_unchanged" {
  command = plan

  assert {
    # deploy.sh names the service account to match this, and annotates it with
    # addon_role_arns.cluster_autoscaler.
    condition = anytrue([
      for c in data.aws_iam_policy_document.assume["ca"].statement[0].condition :
      c.variable == "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:sub" && c.values == tolist(["system:serviceaccount:kube-system:cluster-autoscaler"])
    ])
    error_message = "the Cluster Autoscaler trust must stay kube-system:cluster-autoscaler"
  }
}

run "load_balancer_controller_ingress_is_scoped_to_the_vpc" {
  command = plan

  assert {
    # The one change to the vendored upstream policy: security-group ingress edits are
    # limited to this account and Region's security groups in the cluster VPC. The
    # policy is split across two managed policies to fit the IAM size quota, so the
    # statements are flattened from every instance before they are checked.
    condition = anytrue([
      for s in flatten([for p in aws_iam_policy.lbc : jsondecode(p.policy).Statement]) :
      s.Action == ["ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress"]
      && s.Resource == "arn:aws:ec2:us-east-1:${data.aws_caller_identity.current.account_id}:security-group/*"
      && try(s.Condition.ArnEquals["ec2:Vpc"], "") == "arn:aws:ec2:us-east-1:${data.aws_caller_identity.current.account_id}:vpc/vpc-0123456789abcdef0"
    ])
    error_message = "Load Balancer Controller security-group ingress must be scoped to the cluster VPC"
  }
  assert {
    # No other statement grants security-group ingress changes without a condition.
    condition = alltrue([
      for s in flatten([for p in aws_iam_policy.lbc : jsondecode(p.policy).Statement]) :
      try(s.Condition, null) != null
      if contains(try(tolist(s.Action), [s.Action]), "ec2:AuthorizeSecurityGroupIngress")
    ])
    error_message = "a security-group ingress statement has no condition"
  }
}

run "load_balancer_controller_writes_are_scoped_to_the_account" {
  command = plan

  assert {
    # Listener, rule, WAF association and Shield DeleteProtection writes name ARNs in
    # this account (and Region, where the ARN has one) instead of "*". Statements are
    # flattened from both split policies.
    condition = alltrue([
      for s in flatten([for p in aws_iam_policy.lbc : jsondecode(p.policy).Statement]) : alltrue([
        for r in try(tolist(s.Resource), [s.Resource]) :
        startswith(r, "arn:aws:elasticloadbalancing:us-east-1:${data.aws_caller_identity.current.account_id}:")
        || startswith(r, "arn:aws:waf-regional:us-east-1:${data.aws_caller_identity.current.account_id}:")
        || startswith(r, "arn:aws:wafv2:us-east-1:${data.aws_caller_identity.current.account_id}:regional/")
        || startswith(r, "arn:aws:shield::${data.aws_caller_identity.current.account_id}:protection/")
      ])
      if length(setintersection(try(tolist(s.Action), [s.Action]), [
        "elasticloadbalancing:CreateListener", "elasticloadbalancing:DeleteListener",
        "elasticloadbalancing:CreateRule", "elasticloadbalancing:DeleteRule",
        "elasticloadbalancing:ModifyListener", "elasticloadbalancing:ModifyRule",
        "elasticloadbalancing:AddListenerCertificates", "elasticloadbalancing:RemoveListenerCertificates",
        "elasticloadbalancing:SetRulePriorities",
        "wafv2:AssociateWebACL", "wafv2:DisassociateWebACL",
        "waf-regional:AssociateWebACL", "waf-regional:DisassociateWebACL",
        "shield:DeleteProtection",
      ])) > 0
    ])
    error_message = "a Load Balancer Controller listener, rule, WAF or Shield write is not scoped to this account"
  }
  assert {
    # Only actions without resource-level permissions, and read-only calls, stay on "*"
    # without a condition.
    condition = alltrue([
      for s in flatten([for p in aws_iam_policy.lbc : jsondecode(p.policy).Statement]) : alltrue([
        for a in try(tolist(s.Action), [s.Action]) :
        can(regex(":(Describe|Get|List)", a))
        || contains(["elasticloadbalancing:SetWebAcl", "shield:CreateProtection", "ec2:CreateSecurityGroup"], a)
      ])
      if s.Resource == "*" && try(s.Condition, null) == null
    ])
    error_message = "an unconditioned Resource \"*\" statement grants a write action"
  }
}
