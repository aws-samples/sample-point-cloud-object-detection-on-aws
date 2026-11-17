# Plan-only checks that the bastion is an SSM relay and nothing more. The AWS provider
# is mocked, so this needs no credentials and creates nothing:
#
#   terraform -chdir=infrastructure/modules/bastion init -backend=false
#   terraform -chdir=infrastructure/modules/bastion test
#
# That the module declares no EKS access entry, access policy association or managed
# policy attachment for the bastion role is a property of the configuration itself: a
# test cannot reference a resource that does not exist, so review keeps that one.

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = {
      region = "us-east-1"
    }
  }
  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-0123456789abcdef0"
    }
  }
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  prefix                    = "pointcloud-detection-dev"
  vpc_id                    = "vpc-0123456789abcdef0"
  vpc_cidr                  = "10.0.0.0/16"
  private_subnet_id         = "subnet-0123456789abcdef0"
  cluster_name              = "pointcloud-detection-dev"
  cluster_arn               = "arn:aws:eks:us-east-1:111122223333:cluster/pointcloud-detection-dev"
  cluster_endpoint          = "https://EXAMPLE0123456789.gr7.us-east-1.eks.amazonaws.com"
  cluster_security_group_id = "sg-0123456789abcdef0"
}

run "egress_stays_inside_the_vpc" {
  command = plan

  assert {
    condition     = aws_vpc_security_group_egress_rule.https.cidr_ipv4 == "10.0.0.0/16"
    error_message = "bastion egress must be the VPC CIDR only"
  }
  assert {
    condition     = aws_vpc_security_group_egress_rule.https.cidr_ipv4 != "0.0.0.0/0" && aws_vpc_security_group_egress_rule.https.cidr_ipv6 == null
    error_message = "bastion egress must not reach the internet"
  }
  assert {
    condition     = aws_vpc_security_group_egress_rule.https.from_port == 443 && aws_vpc_security_group_egress_rule.https.to_port == 443 && aws_vpc_security_group_egress_rule.https.ip_protocol == "tcp"
    error_message = "bastion egress must be TCP 443 only"
  }
  assert {
    condition     = aws_instance.bastion.associate_public_ip_address == false && aws_instance.bastion.metadata_options[0].http_tokens == "required"
    error_message = "no public IP, IMDSv2 only"
  }
}

run "ssm_reaches_the_relay_through_interface_endpoints" {
  command = plan

  assert {
    condition     = toset(keys(aws_vpc_endpoint.ssm)) == toset(["ssm", "ssmmessages", "ec2messages"])
    error_message = "Session Manager without internet access needs the ssm, ssmmessages and ec2messages endpoints"
  }
  assert {
    condition = alltrue([
      for k, e in aws_vpc_endpoint.ssm :
      e.service_name == "com.amazonaws.us-east-1.${k}" && e.vpc_endpoint_type == "Interface" && e.private_dns_enabled && e.subnet_ids == toset(["subnet-0123456789abcdef0"])
    ])
    error_message = "endpoints must be private-DNS interface endpoints in the bastion's subnet"
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.ssm_endpoints_from_bastion.from_port == 443 && aws_vpc_security_group_ingress_rule.ssm_endpoints_from_bastion.cidr_ipv4 == null
    error_message = "the endpoint security group admits 443 from the bastion's group only"
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.cluster_from_bastion.security_group_id == "sg-0123456789abcdef0" && aws_vpc_security_group_ingress_rule.cluster_from_bastion.from_port == 443
    error_message = "the cluster security group must admit 443 from the bastion"
  }
}

run "host_installs_nothing_and_holds_no_cluster_access" {
  command = plan

  assert {
    condition     = !strcontains(aws_instance.bastion.user_data, "curl") && !strcontains(aws_instance.bastion.user_data, "dnf") && !strcontains(aws_instance.bastion.user_data, "| bash")
    error_message = "the relay must not download or install anything at boot"
  }
  assert {
    condition     = aws_instance.bastion.user_data_replace_on_change
    error_message = "a changed user_data must rebuild the host, not restart it"
  }
  assert {
    condition = toset(flatten([
      for st in jsondecode(aws_iam_role_policy.session_manager.policy).Statement : [st.Action]
    ])) == toset(["ssm:UpdateInstanceInformation", "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel", "ec2messages:AcknowledgeMessage", "ec2messages:DeleteMessage", "ec2messages:FailMessage", "ec2messages:GetEndpoint", "ec2messages:GetMessages", "ec2messages:SendReply"])
    error_message = "the bastion role may run SSM Agent's session channels and nothing else, in particular no ssm:GetParameter"
  }
}

# apply, against the mock: the instance ID in the commands is known only after apply.
run "relay_commands_point_at_the_private_endpoint" {
  command = apply

  assert {
    condition     = output.cluster_endpoint_host == "EXAMPLE0123456789.gr7.us-east-1.eks.amazonaws.com"
    error_message = "the relay forwards to the endpoint host"
  }
  assert {
    condition     = strcontains(output.relay_commands, "AWS-StartPortForwardingSessionToRemoteHost") && strcontains(output.relay_commands, "\"host\":[\"EXAMPLE0123456789.gr7.us-east-1.eks.amazonaws.com\"]") && strcontains(output.relay_commands, "--tls-server-name EXAMPLE0123456789.gr7.us-east-1.eks.amazonaws.com")
    error_message = "relay_commands must port-forward to the endpoint host and keep it as the TLS server name"
  }
}

run "operators_get_port_forwarding_and_no_shell" {
  command = apply

  assert {
    condition = alltrue([
      for st in jsondecode(output.operator_policy_json).Statement :
      st.Action != "ssm:StartSession" || (
        toset(st.Resource) == toset([aws_instance.bastion.arn, "arn:aws:ssm:us-east-1::document/AWS-StartPortForwardingSessionToRemoteHost"])
        && st.Condition.BoolIfExists["ssm:SessionDocumentAccessCheck"] == "true"
      )
    ])
    error_message = "StartSession must name only this instance and the port-forwarding document, with the document access check"
  }
  assert {
    condition     = length([for st in jsondecode(output.operator_policy_json).Statement : st if st.Action == "ssm:StartSession"]) == 1
    error_message = "one StartSession statement"
  }
}

run "egress_to_anywhere_is_rejected" {
  command = plan

  variables {
    vpc_cidr = "0.0.0.0/0"
  }

  expect_failures = [var.vpc_cidr]
}
