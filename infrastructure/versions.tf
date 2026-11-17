terraform {
  # 1.10 is the floor because backend.tf uses the S3 backend's native state locking
  # (`use_lockfile`), added in that release. Terraform 1.5 would fail at init with an
  # unsupported-argument error rather than a version message, so the constraint is
  # raised here to say why. 1.5 remains the floor for `import` blocks, which the
  # adoption tooling in scripts/plan-adoption.sh generates.
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.37"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.5"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
  }
}
