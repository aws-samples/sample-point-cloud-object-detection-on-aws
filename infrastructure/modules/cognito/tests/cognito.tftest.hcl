# Plan-only checks of the sign-in configuration the ALB depends on. The AWS provider
# is mocked, so this needs no credentials and creates nothing:
#
#   terraform -chdir=infrastructure/modules/cognito init -backend=false
#   terraform -chdir=infrastructure/modules/cognito test

mock_provider "aws" {
}

variables {
  prefix   = "pointcloud-detection-dev"
  region   = "us-east-1"
  app_host = "PCD.Example.com"
}

run "alb_client_matches_the_elb_requirements" {
  command = plan

  assert {
    # The ELB guide: callback must be all lowercase, on the host users open.
    condition     = aws_cognito_user_pool_client.alb.callback_urls == toset(["https://pcd.example.com/oauth2/idpresponse"])
    error_message = "callback URL must be https://<lowercase app_host>/oauth2/idpresponse"
  }
  assert {
    condition     = aws_cognito_user_pool_client.alb.generate_secret && aws_cognito_user_pool_client.alb.allowed_oauth_flows == toset(["code"])
    error_message = "the ALB needs a confidential client using the code grant"
  }
  assert {
    condition     = aws_cognito_user_pool_client.alb.allowed_oauth_scopes == toset(["openid", "email"])
    error_message = "scopes must match the ALB's auth-scope (openid email)"
  }
  assert {
    condition     = aws_cognito_user_pool_client.alb.prevent_user_existence_errors == "ENABLED" && aws_cognito_user_pool_client.alb.enable_token_revocation
    error_message = "user-existence errors must be suppressed and token revocation on"
  }
  assert {
    # Refresh token covers the whole ALB session, or users are sent to sign in mid-upload.
    condition     = aws_cognito_user_pool_client.alb.refresh_token_validity == 12 && aws_cognito_user_pool_client.alb.token_validity_units[0].refresh_token == "hours"
    error_message = "refresh token must last session_hours"
  }
}

run "pool_is_invite_only_and_destroyable" {
  command = plan

  assert {
    condition     = aws_cognito_user_pool.this.admin_create_user_config[0].allow_admin_create_user_only
    error_message = "self sign-up must be off"
  }
  assert {
    condition     = aws_cognito_user_pool.this.deletion_protection == "INACTIVE"
    error_message = "deletion protection must be off so destroy.sh works"
  }
  assert {
    condition     = aws_cognito_user_pool.this.password_policy[0].minimum_length >= 14
    error_message = "password policy weakened"
  }
  assert {
    # Cognito rejects an invite template under 6 characters, and the provider sends an
    # empty SMS one unless it is set: CreateUserPool failed on it on a sandbox account.
    condition = alltrue([
      for m in [
        aws_cognito_user_pool.this.admin_create_user_config[0].invite_message_template[0].sms_message,
        aws_cognito_user_pool.this.admin_create_user_config[0].invite_message_template[0].email_message,
      ] : length(m) >= 6 && strcontains(m, "{username}") && strcontains(m, "{####}")
    ])
    error_message = "invite templates must be set, and carry {username} and {####}"
  }
  assert {
    condition     = length(aws_cognito_user_pool.this.admin_create_user_config[0].invite_message_template[0].sms_message) <= 140
    error_message = "Cognito caps the SMS invite template at 140 characters"
  }
}

run "domain_prefix_is_valid_and_hides_the_account" {
  command = plan

  assert {
    condition     = can(regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", aws_cognito_user_pool_domain.this.domain))
    error_message = "domain prefix does not match Cognito's pattern"
  }
  assert {
    condition     = !strcontains(aws_cognito_user_pool_domain.this.domain, data.aws_caller_identity.current.account_id)
    error_message = "the public sign-in URL must not contain the account ID"
  }
}

run "mfa_value_is_checked" {
  command = plan

  variables {
    mfa_configuration = "OFF"
  }

  expect_failures = [var.mfa_configuration]
}

run "mfa_is_required_by_default" {
  command = plan

  assert {
    # OPTIONAL is password-only in practice: the hosted page never prompts anyone to
    # register an authenticator. Internal guidance treats missing MFA as launch-blocking.
    condition     = aws_cognito_user_pool.this.mfa_configuration == "ON"
    error_message = "the default must require TOTP (mfa_configuration = ON)"
  }
}

run "alb_client_reads_only_what_the_alb_needs" {
  command = plan

  assert {
    # The ALB asks for openid and email; the application reads no claim. sub is always
    # in the ID token and the userInfo response, so it is not listed.
    condition     = aws_cognito_user_pool_client.alb.read_attributes == toset(["email", "email_verified"])
    error_message = "the ALB client must read email and email_verified only"
  }
}

run "failed_sign_ins_raise_an_alarm" {
  command = plan

  variables {
    alarm_topic_arn = "arn:aws:sns:us-east-1:111122223333:pointcloud-detection-dev-alarms"
  }

  assert {
    # Failures = attempts (SampleCount) - successes (Sum) of SignInSuccesses.
    condition = anytrue([
      for q in aws_cloudwatch_metric_alarm.failed_sign_ins.metric_query :
      q.return_data == true && q.expression == "attempts - successes"
      ]) && anytrue([
      for q in aws_cloudwatch_metric_alarm.failed_sign_ins.metric_query :
      q.id == "attempts" && q.metric[0].metric_name == "SignInSuccesses" && q.metric[0].namespace == "AWS/Cognito" && q.metric[0].stat == "SampleCount" && q.metric[0].period == 300
      ]) && anytrue([
      for q in aws_cloudwatch_metric_alarm.failed_sign_ins.metric_query :
      q.id == "successes" && q.metric[0].metric_name == "SignInSuccesses" && q.metric[0].stat == "Sum" && q.metric[0].period == 300
    ])
    error_message = "the alarm must compute failed sign-ins as SampleCount minus Sum of SignInSuccesses over 5 minutes"
  }
  assert {
    condition = alltrue([
      for q in aws_cloudwatch_metric_alarm.failed_sign_ins.metric_query :
      toset(keys(q.metric[0].dimensions)) == toset(["UserPool", "UserPoolClient"]) if q.expression == null || q.expression == ""
    ])
    error_message = "the metrics must be per user pool and app client"
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.failed_sign_ins.treat_missing_data == "notBreaching" && aws_cloudwatch_metric_alarm.failed_sign_ins.threshold == 10
    error_message = "no sign-ins is not a breach; the default threshold is 10"
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.failed_sign_ins.alarm_actions == toset(["arn:aws:sns:us-east-1:111122223333:pointcloud-detection-dev-alarms"])
    error_message = "the alarm must notify the monitoring topic when there is one"
  }
}

run "failed_sign_in_alarm_without_a_topic" {
  command = plan

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.failed_sign_ins.alarm_actions) == 0
    error_message = "with no alarm topic the alarm has no action"
  }
}
