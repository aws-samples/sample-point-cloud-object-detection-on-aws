locals {
  repositories = ["${var.prefix}-backend", "${var.prefix}-frontend", "${var.prefix}-training"]
}

resource "aws_ecr_repository" "repos" {
  for_each = toset(local.repositories)
  name     = each.value

  # @secure_recommendation: IMMUTABLE tags prevent an attacker (or accident) from
  # overwriting a released tag (e.g. v1.0) with a different image — supply-chain integrity.
  image_tag_mutability = "IMMUTABLE"

  # @secure_recommendation: scan every image on push for known CVEs (ECR security baseline).
  image_scanning_configuration {
    scan_on_push = true
  }

  # @secure_recommendation: SSE-KMS with the project's ECR CMK instead of the default
  # AES-256, so image layers are under a key this stack controls and every decrypt is
  # in CloudTrail.
  #
  # ECR fixes a repository's encryption when it is created, so on a stack applied
  # before this block existed the change REPLACES each repository, and its images go
  # with it. The provider deletes the old repository with the force_delete value held
  # in state, which is unset for those repositories, so that one replacement fails on a
  # non-empty repository. Delete the three repositories once before upgrading
  # (`aws ecr delete-repository --force --repository-name <name>`); the apply then
  # recreates them encrypted, and deploy.sh pushes the backend and frontend images
  # again. A training image has to be pushed again by hand.
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }

  # Lets `terraform destroy` remove a repository that still holds images, instead of
  # failing on RepositoryNotEmptyException. Takes effect once it is in state.
  force_delete = true

  tags = {
    Name = each.value
  }
}

resource "aws_ecr_lifecycle_policy" "repos" {
  for_each   = toset(local.repositories)
  repository = aws_ecr_repository.repos[each.value].name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 10 tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Remove untagged images after 3 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 3
        }
        action = { type = "expire" }
      }
    ]
  })
}
