# Pinned here as well as in the root so the module's own test (tests/) resolves the
# same major version the stack is locked to (.terraform.lock.hcl at the root).
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.37"
    }
  }
}
