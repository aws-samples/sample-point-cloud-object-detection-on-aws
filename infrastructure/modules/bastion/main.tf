# @secure_recommendation: SSM relay to a PRIVATE EKS endpoint. The instance is a
# port-forwarding hop and nothing else: no inbound security-group rules, no public IP,
# no Kubernetes permissions and no tools. Operators run kubectl and helm on their own
# machines, with their own IAM identity, through a Session Manager port-forwarding
# session to the EKS API (AWS-StartPortForwardingSessionToRemoteHost; the
# relay_commands output has the commands). The Kubernetes API authorizes, and the
# control-plane audit log records, each operator's own access entry rather than one
# shared role, and ssm:StartSession on this instance grants no cluster access by
# itself.
#
# Egress is TCP 443 to the VPC CIDR only: the EKS API endpoint's network interfaces and
# the three Systems Manager interface endpoints below, which is all SSM Agent and the
# relay need. Nothing on the host reaches the internet. Link-local services (instance
# metadata, the Amazon DNS resolver, Amazon Time Sync) are not filtered by security
# groups, so credentials, name resolution and time still work.
#
# Note that Session Manager does not log the content of port-forwarding sessions.
# CloudTrail records each StartSession call, and the EKS audit log records the
# Kubernetes requests made through it under the operator's identity.

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  region = data.aws_region.current.region

  # The EKS API endpoint is https://<host>; the relay forwards to that host on 443.
  # Inside the VPC the host resolves to the private endpoint's addresses.
  cluster_endpoint_host = trimsuffix(trimprefix(var.cluster_endpoint, "https://"), "/")

  # Session Manager on an instance without internet access needs these three
  # interface endpoints (Systems Manager guide, VPC endpoints). ec2messages is kept
  # for SSM Agent versions older than 3.3.40.0, which use it instead of ssmmessages.
  ssm_endpoint_services = toset(["ssm", "ssmmessages", "ec2messages"])

  # Local port the operator's end of the relay listens on.
  relay_local_port = 8443
}

resource "aws_security_group" "bastion" {
  name_prefix = "${var.prefix}-bastion-"
  description = "SSM relay to the private EKS API (egress to the VPC only)"
  vpc_id      = var.vpc_id

  tags = { Name = "${var.prefix}-bastion" }

  lifecycle {
    create_before_destroy = true
  }
}

# @secure_recommendation: egress restricted to HTTPS inside the VPC (the SSM interface
# endpoints and the EKS API endpoint). No ingress rules.
resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.bastion.id
  description       = "HTTPS to the SSM interface endpoints and the EKS API endpoint, inside the VPC"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = var.vpc_cidr
}

# Allow the relay to reach the EKS API endpoint ENIs.
resource "aws_vpc_security_group_ingress_rule" "cluster_from_bastion" {
  security_group_id            = var.cluster_security_group_id
  description                  = "Allow SSM bastion to reach the EKS API"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.bastion.id
}

# ---------------------------------------------------------------------------
# Systems Manager interface endpoints, so the relay needs no route to the internet.
# ---------------------------------------------------------------------------
# In the bastion's own subnet only: each endpoint is billed per Availability Zone, and
# the single-instance relay is no more available than its one AZ anyway. Private DNS
# makes the standard ssm.<region>.amazonaws.com names resolve to these endpoints across
# the whole VPC. The endpoint security group admits only the bastion, which is also the
# only instance in the stack with a Systems Manager role (the node role has none).
resource "aws_security_group" "ssm_endpoints" {
  name_prefix = "${var.prefix}-ssm-endpoints-"
  description = "Systems Manager interface endpoints for the SSM relay"
  vpc_id      = var.vpc_id

  tags = { Name = "${var.prefix}-ssm-endpoints" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "ssm_endpoints_from_bastion" {
  security_group_id            = aws_security_group.ssm_endpoints.id
  description                  = "HTTPS from the SSM relay"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.bastion.id
}

resource "aws_vpc_endpoint" "ssm" {
  for_each = local.ssm_endpoint_services

  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${local.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [var.private_subnet_id]
  security_group_ids  = [aws_security_group.ssm_endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${var.prefix}-${each.key}" }
}

data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "bastion" {
  name               = "${var.prefix}-bastion"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# @secure_recommendation: what SSM Agent needs for Session Manager and nothing else (no
# SSH keys, no open ports). It is the role's only permission. Not the managed
# AmazonSSMManagedInstanceCore, which also allows ssm:GetParameter and
# GetParameters on every parameter: anyone who got a shell here would read the
# account's Parameter Store, SecureStrings included. The ssmmessages and ec2messages
# actions support no resource-level scoping, hence "*" for those. The relay holds no EKS access entry and no EKS
# IAM permission: it forwards TLS it cannot read, and the operator at the other end
# authenticates to the cluster as themselves.
resource "aws_iam_role_policy" "session_manager" {
  name = "session-manager-agent"
  role = aws_iam_role.bastion.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # The one action here that supports a resource: instances of this account and
        # Region. Not this instance's own ARN, which does not exist when the policy
        # is written and would make its first registration wait for a retry.
        Sid      = "RegisterTheInstance"
        Effect   = "Allow"
        Action   = "ssm:UpdateInstanceInformation"
        Resource = "arn:${data.aws_partition.current.partition}:ec2:${local.region}:${data.aws_caller_identity.current.account_id}:instance/*"
      },
      {
        Sid    = "SessionManagerChannels"
        Effect = "Allow"
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ]
        Resource = "*"
      },
      {
        # For SSM Agent versions older than 3.3.40.0; see ssm_endpoint_services.
        Sid    = "OlderAgents"
        Effect = "Allow"
        Action = [
          "ec2messages:AcknowledgeMessage",
          "ec2messages:DeleteMessage",
          "ec2messages:FailMessage",
          "ec2messages:GetEndpoint",
          "ec2messages:GetMessages",
          "ec2messages:SendReply",
        ]
        Resource = "*"
      },
    ]
  })
}

# What an operator needs to use the relay, and all of it: a session on this instance
# with the port-forwarding document only. ssm:StartSession on the instance alone would
# also allow the default shell document, a shell on the relay under its role. With
# ssm:SessionDocumentAccessCheck, Session Manager checks the caller may use the
# document named, so the default shell document is refused. Attach it to the operators'
# own roles (the operator_policy_json output); this stack creates no operator roles.
locals {
  operator_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "PortForwardToTheEksEndpointOnly"
        Effect = "Allow"
        Action = "ssm:StartSession"
        Resource = [
          aws_instance.bastion.arn,
          "arn:${data.aws_partition.current.partition}:ssm:${local.region}::document/AWS-StartPortForwardingSessionToRemoteHost",
        ]
        Condition = {
          BoolIfExists = { "ssm:SessionDocumentAccessCheck" = "true" }
        }
      },
      {
        Sid      = "OwnSessionsOnly"
        Effect   = "Allow"
        Action   = ["ssm:TerminateSession", "ssm:ResumeSession"]
        Resource = "arn:${data.aws_partition.current.partition}:ssm:*:${data.aws_caller_identity.current.account_id}:session/$${aws:userid}-*"
      },
    ]
  }
}

resource "aws_iam_instance_profile" "bastion" {
  name = "${var.prefix}-bastion"
  role = aws_iam_role.bastion.name
}

resource "aws_instance" "bastion" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  subnet_id              = var.private_subnet_id
  vpc_security_group_ids = [aws_security_group.bastion.id]
  iam_instance_profile   = aws_iam_instance_profile.bastion.name
  ebs_optimized          = true

  # @secure_recommendation: no public IP; enforce IMDSv2; encrypt the root volume.
  associate_public_ip_address = false

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    encrypted   = true
    volume_size = 20
    volume_type = "gp3"
  }

  # Nothing to install: the relay needs only SSM Agent, which the Amazon Linux 2023 AMI
  # ships with, so nothing is downloaded at boot. An earlier version's user_data
  # installed kubectl and helm as root from unpinned, unverified sources. This is an
  # empty cloud-config rather than no user_data because the provider treats an unset
  # user_data as "keep the current one", so a bastion that ran that script would keep
  # it. A changed value, with user_data_replace_on_change, rebuilds such a host from
  # the AMI instead of restarting it with those binaries still on disk.
  user_data                   = "#cloud-config\n# SSM relay: nothing to install. SSM Agent ships with the AMI.\n"
  user_data_replace_on_change = true

  tags = { Name = "${var.prefix}-bastion" }

  depends_on = [aws_vpc_endpoint.ssm]
}
