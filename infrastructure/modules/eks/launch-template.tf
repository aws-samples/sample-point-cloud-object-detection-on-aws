# @secure_recommendation: launch templates for the managed node groups enforce IMDSv2
# (http_tokens=required) with a hop limit of 1 so pods cannot reach the instance metadata
# service and steal the node IAM role (pods use IRSA instead). Root EBS volumes are
# encrypted. No image_id is set so EKS still selects the correct EKS-optimized AMI for the
# node group's ami_type and injects the bootstrap user-data.

resource "aws_launch_template" "cpu" {
  name_prefix = "${var.cluster_name}-cpu-"

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 100
      volume_type           = "gp3"
      encrypted             = "true"
      delete_on_termination = "true"
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${var.cluster_name}-cpu-node" }
  }
}

# One launch template per GPU pool: root_volume_size is per-pool, so a single
# shared template cannot express pools with different disk needs (model weights
# on a p4d want more room than a g4dn inference node).
resource "aws_launch_template" "gpu" {
  for_each = local.gpu_pools_active

  name_prefix = local.gpu_pool_lt_name_prefix[each.key]

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = each.value.root_volume_size
      volume_type           = "gp3"
      encrypted             = "true"
      delete_on_termination = "true"
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${var.cluster_name}-gpu-${each.key}-node" }
  }
}
