# Pinned here as well as in the root so the module's own test (tests/) resolves the
# same major version the stack is locked to. 5.100 is the locked release, and is new
# enough for aws_cognito_user_pool_domain.managed_login_version.
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.37"
    }
  }
}
