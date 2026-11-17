data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  # Every bucket is created in this account's regional namespace: a name ending in
  # -<account>-<region>-an that S3 lets no other account create, before or after this
  # stack deletes it. In the shared global namespace anyone who can guess a name can
  # register it first and receive its uploads, results or logs (bucket squatting, T62).
  # 63 is S3's limit on a bucket name, suffix included.
  name_suffix  = "${data.aws_caller_identity.current.account_id}-${data.aws_region.current.region}-an"
  logs_bucket  = "${var.prefix}-logs-${local.name_suffix}"
  data_buckets = { for k in keys(local.buckets) : k => "${var.prefix}-${k}-${local.name_suffix}" }

  # @secure_recommendation: versioning enabled on ALL data buckets (S3 security baseline) so
  # object overwrites/deletes are recoverable and there is a tamper-evident history.
  #
  # noncurrent_days is how long an overwritten or deleted object's old version is kept,
  # which is also how long a deletion takes to become an erasure. The models bucket
  # keeps old checkpoints longer, for rollback, and never expires the current one.
  buckets = {
    input  = { expiration_days = 30, noncurrent_days = 7, versioning = true, cors = true }
    output = { expiration_days = 90, noncurrent_days = 7, versioning = true, cors = false }
    tiles  = { expiration_days = 30, noncurrent_days = 7, versioning = true, cors = true }
    models = { expiration_days = null, noncurrent_days = 30, versioning = true, cors = false }
  }
}

# ---------------------------------------------------------------------------
# Account-level Block Public Access: opt-in, off by default.
#
# This is one setting for the whole account, not part of this stack, so the stack does
# not manage it unless asked. Managed unconditionally, `terraform destroy` called
# DeletePublicAccessBlock and switched it off for every bucket in the account,
# including a setting the adopter had before deploying, and two stacks in one account
# fought over it. The per-bucket blocks below stay on regardless.
#
# Check the account setting with:
#   aws s3control get-public-access-block --account-id <account-id>
#
# With manage_account_public_access_block = true this resource sets all four flags.
# prevent_destroy makes Terraform refuse to delete it, whether by destroy or by turning
# the variable off: remove it from state instead, which leaves the setting in place:
#   terraform state rm 'module.s3.aws_s3_account_public_access_block.account_opt_in[0]'
# destroy.sh does that before it destroys.
# ---------------------------------------------------------------------------
resource "aws_s3_account_public_access_block" "account_opt_in" {
  count = var.manage_account_public_access_block ? 1 : 0

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  lifecycle {
    prevent_destroy = true
  }
}

# The resource this stack used to manage unconditionally. Stacks applied before the
# change hold it in state; this forgets it there without calling DeletePublicAccessBlock,
# so the account keeps whatever setting it has.
removed {
  from = aws_s3_account_public_access_block.account

  lifecycle {
    destroy = false
  }
}

# ---------------------------------------------------------------------------
# Dedicated server access logging bucket. Not itself logged (avoids recursion).
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "logs" {
  # False positive: S3 server access log delivery requires the destination bucket to
  # use SSE-S3; with SSE-KMS default encryption S3 may deliver logs you cannot read.
  #checkov:skip=CKV_AWS_145:Server access log destination buckets must use SSE-S3, not SSE-KMS (S3 server access logging guide)
  bucket           = local.logs_bucket
  bucket_namespace = "account-regional"

  tags = {
    Name        = local.logs_bucket
    Environment = var.environment
  }

  lifecycle {
    # S3 offers account regional namespaces in every Region except Middle East (Bahrain)
    # and Middle East (UAE) (S3 User Guide, "Namespaces for general purpose buckets").
    precondition {
      condition     = !contains(["me-south-1", "me-central-1"], data.aws_region.current.region)
      error_message = "S3 account regional namespaces are not available in me-south-1 or me-central-1; deploy the stack in another Region."
    }
    precondition {
      condition     = length(local.logs_bucket) <= 63
      error_message = "The access-logs bucket name, with its account and Region suffix, is longer than S3's 63-character limit; shorten project_name or environment."
    }
  }
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      # S3 server access logging delivery does not support SSE-KMS; use SSE-S3 here.
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "expire-logs"
    status = "Enabled"
    filter {}
    expiration {
      days = var.log_retention_days
    }

    # The bucket is versioned, so `expiration` only adds a delete marker and the log
    # itself stays on as a noncurrent version. Expire those too, or access logs, which
    # carry requester IPs and object keys, are kept indefinitely.
    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
}

# Allow the S3 logging service to write access logs into the logs bucket.
data "aws_iam_policy_document" "logs" {
  statement {
    sid       = "S3ServerAccessLogsPolicy"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.logs.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }
    # @secure_recommendation: confused-deputy guard, as in the S3 server access logging
    # guide: the logging service may write here only on behalf of this stack's data
    # buckets, in this account. A bucket ARN carries no account ID, hence both keys.
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = [for b in aws_s3_bucket.buckets : b.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  # @secure_recommendation: deny any non-TLS access (enforce aws:SecureTransport).
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.logs.arn, "${aws_s3_bucket.logs.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.logs.id
  policy = data.aws_iam_policy_document.logs.json

  depends_on = [aws_s3_bucket_public_access_block.logs]
}

# ---------------------------------------------------------------------------
# Data buckets: input / output / tiles / models
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "buckets" {
  for_each         = local.buckets
  bucket           = local.data_buckets[each.key]
  bucket_namespace = "account-regional"

  tags = {
    Name        = local.data_buckets[each.key]
    Environment = var.environment
  }

  lifecycle {
    precondition {
      condition     = length(local.data_buckets[each.key]) <= 63
      error_message = "A data bucket name, with its account and Region suffix, is longer than S3's 63-character limit; shorten project_name or environment."
    }
  }
}

# @secure_recommendation: disable ACLs (BucketOwnerEnforced) per the S3 security baseline.
resource "aws_s3_bucket_ownership_controls" "buckets" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.buckets[each.key].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "buckets" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.buckets[each.key].id

  versioning_configuration {
    status = "Enabled"
  }
}

# @secure_recommendation: SSE-KMS with a customer-managed key (sse_algorithm=aws:kms
# with a specified key). bucket_key_enabled reduces KMS request costs.
resource "aws_s3_bucket_server_side_encryption_configuration" "buckets" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.buckets[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "buckets" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.buckets[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# @secure_recommendation: route server access logs to the dedicated logs bucket.
resource "aws_s3_bucket_logging" "buckets" {
  for_each      = local.buckets
  bucket        = aws_s3_bucket.buckets[each.key].id
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "logs/${each.key}/"
}

# Every data bucket gets a rule. The models bucket has no `expiration`, because the
# current checkpoint must never expire, only noncurrent versions of it.
resource "aws_s3_bucket_lifecycle_configuration" "buckets" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.buckets[each.key].id

  rule {
    id     = each.value.expiration_days != null ? "expire-after-${each.value.expiration_days}-days" : "expire-noncurrent-versions"
    status = "Enabled"
    filter {}

    dynamic "expiration" {
      for_each = each.value.expiration_days != null ? [each.value.expiration_days] : []
      content {
        days = expiration.value
      }
    }

    # These buckets are versioned, so `expiration` alone, or an API delete, only adds a
    # delete marker: the customer data it was meant to remove stays on as a noncurrent
    # version, indefinitely. Expire those too, after a short window for undoing an
    # overwrite.
    noncurrent_version_expiration {
      noncurrent_days = each.value.noncurrent_days
    }

    # Parts of an upload that was started and never completed or aborted are
    # invisible to listings and to `expiration`, and are billed until removed.
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
}

# @secure_recommendation: CORS origins are an explicit allowlist (var.cors_allowed_origins),
# not a wildcard, to prevent arbitrary web origins from driving the presigned-upload flow.
# The wildcard is rejected by a validation block on that variable, and an empty list
# creates no CORS rule at all rather than falling back to a permissive one -- so the
# unconfigured state is "browser uploads do not work yet", not "any origin may upload".
resource "aws_s3_bucket_cors_configuration" "buckets" {
  for_each = { for k, v in local.buckets : k => v if v.cors && length(var.cors_allowed_origins) > 0 }
  bucket   = aws_s3_bucket.buckets[each.key].id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["PUT", "POST", "GET"]
    allowed_origins = var.cors_allowed_origins
    # The browser completes a multipart upload with each part's ETag, and hides that
    # header from cross-origin script unless it is exposed here (upload.ts).
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}

# @secure_recommendation: deny all non-TLS (HTTP) requests to every data bucket.
data "aws_iam_policy_document" "tls_only" {
  for_each = local.buckets

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.buckets[each.key].arn, "${aws_s3_bucket.buckets[each.key].arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "tls_only" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.buckets[each.key].id
  policy   = data.aws_iam_policy_document.tls_only[each.key].json

  depends_on = [aws_s3_bucket_public_access_block.buckets]
}
