# Plan-only checks of who administers the cluster. The AWS and TLS providers are
# mocked, so this needs no credentials and creates nothing:
#
#   terraform -chdir=infrastructure/modules/eks init -backend=false
#   terraform -chdir=infrastructure/modules/eks test

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      arn = "arn:aws:sts::111122223333:assumed-role/Operator/session"
    }
  }
  # What the provider resolves an assumed-role session to: the role's own ARN.
  mock_data "aws_iam_session_context" {
    defaults = {
      issuer_arn = "arn:aws:iam::111122223333:role/Operator"
    }
  }
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }
  mock_data "aws_ec2_instance_type_offerings" {
    defaults = {
      locations = ["us-east-1a", "us-east-1b"]
    }
  }
}

mock_provider "tls" {}

variables {
  cluster_name       = "pointcloud-detection-dev"
  cluster_version    = "1.33"
  vpc_id             = "vpc-0123456789abcdef0"
  private_subnet_ids = ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"]
  public_subnet_ids  = ["subnet-0123456789abcdef2"]
  cpu_instance_types = ["m5.xlarge"]
  cpu_min_size       = 1
  cpu_max_size       = 2
  cpu_desired_size   = 1
  kms_key_arn        = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000"
  app_client_group   = "pointcloud-detection-app-clients"
  gpu_node_pools = {
    g4dn-xl = {
      instance_types     = ["g4dn.xlarge"]
      gpu_count_per_node = 1
      priority           = 100
    }
  }
}

run "the_api_server_has_no_public_endpoint" {
  command = plan

  assert {
    condition     = aws_eks_cluster.main.vpc_config[0].endpoint_public_access == false && aws_eks_cluster.main.vpc_config[0].endpoint_private_access == true
    error_message = "the cluster API endpoint must be private only"
  }
}

run "the_cluster_role_cannot_delegate_the_secrets_key" {
  command = plan

  assert {
    condition     = !contains(jsondecode(aws_iam_role_policy.cluster_kms.policy).Statement[0].Action, "kms:CreateGrant")
    error_message = "the cluster role uses the secrets key and must not be able to grant it to others"
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.cluster_kms.policy).Statement[0].Resource == "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000"
    error_message = "the cluster role's KMS permissions must be scoped to the secrets key"
  }
}

run "no_implicit_creator_admin" {
  command = plan

  assert {
    condition     = aws_eks_cluster.main.access_config[0].bootstrap_cluster_creator_admin_permissions == false
    error_message = "EKS must not bootstrap the cluster creator as an implicit admin"
  }
  assert {
    condition     = aws_eks_cluster.main.vpc_config[0].endpoint_private_access
    error_message = "the private endpoint must stay on; the SSM relay depends on it"
  }
}

run "the_terraform_caller_is_the_default_admin" {
  command = plan

  assert {
    condition     = toset(keys(aws_eks_access_entry.cluster_admin)) == toset(["arn:aws:iam::111122223333:role/Operator"])
    error_message = "with no cluster_admin_principal_arns, the admin is the caller's role (not its session ARN)"
  }
  assert {
    condition = alltrue([
      for a in aws_eks_access_policy_association.cluster_admin :
      a.policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy" && a.access_scope[0].type == "cluster"
    ])
    error_message = "each admin gets AmazonEKSClusterAdminPolicy at cluster scope"
  }
}

# Applies only the record of the first applier: the mocked cluster could not be.
run "first_applier_is_recorded" {
  command = apply

  plan_options {
    target = [terraform_data.first_admin]
  }

  assert {
    condition     = terraform_data.first_admin.input == "arn:aws:iam::111122223333:role/Operator"
    error_message = "the first applier's role is recorded"
  }
}

# A second operator, a CI role or a break-glass role applies later. The default admin
# stays the first applier instead of moving to the new caller.
run "a_later_caller_does_not_replace_the_first_admin" {
  command = plan

  override_data {
    target = data.aws_iam_session_context.current
    values = {
      issuer_arn = "arn:aws:iam::111122223333:role/SecondOperator"
    }
  }

  assert {
    condition     = toset(keys(aws_eks_access_entry.cluster_admin)) == toset(["arn:aws:iam::111122223333:role/Operator"])
    error_message = "a later apply by another identity must keep the first applier as the default admin"
  }
}

run "explicit_admins_replace_the_default" {
  command = plan

  variables {
    cluster_admin_principal_arns = ["arn:aws:iam::111122223333:role/platform/ClusterAdmin", "arn:aws:iam::111122223333:role/Deployer"]
  }

  assert {
    condition     = toset(keys(aws_eks_access_entry.cluster_admin)) == toset(["arn:aws:iam::111122223333:role/platform/ClusterAdmin", "arn:aws:iam::111122223333:role/Deployer"])
    error_message = "explicit cluster_admin_principal_arns must be the admins, and the caller is not added"
  }
  assert {
    condition     = length(aws_eks_access_policy_association.cluster_admin) == 2
    error_message = "one policy association per admin"
  }
}

run "an_admin_cannot_also_be_an_app_client" {
  command = plan

  variables {
    app_client_principal_arns = ["arn:aws:iam::111122223333:role/Operator"]
  }

  expect_failures = [aws_eks_access_entry.cluster_admin]
}

run "app_clients_get_the_group_and_no_policy" {
  command = plan

  variables {
    app_client_principal_arns = ["arn:aws:iam::111122223333:role/Viewer"]
  }

  assert {
    condition     = aws_eks_access_entry.app_clients["arn:aws:iam::111122223333:role/Viewer"].kubernetes_groups == toset(["pointcloud-detection-app-clients"])
    error_message = "app clients map to the port-forward group only"
  }
}

run "a_pre_turing_gpu_pool_is_refused" {
  command = plan

  variables {
    gpu_node_pools = {
      v100 = {
        instance_types     = ["p3.2xlarge"]
        gpu_count_per_node = 1
        priority           = 100
      }
    }
  }

  expect_failures = [var.gpu_node_pools]
}
