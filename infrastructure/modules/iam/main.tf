locals {
  roles = {
    backend = {
      sa_name = "backend-sa"
      policy  = data.aws_iam_policy_document.backend.json
    }
    worker = {
      sa_name = "worker-sa"
      policy  = data.aws_iam_policy_document.worker.json
    }
    training = {
      sa_name = "training-sa"
      policy  = data.aws_iam_policy_document.training.json
    }
  }
}

resource "aws_iam_role" "irsa" {
  for_each = local.roles
  name     = "${var.cluster_name}-${each.key}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = var.oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${var.oidc_issuer}:sub" = "system:serviceaccount:${var.namespace}:${each.value.sa_name}"
          "${var.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "irsa" {
  for_each = local.roles
  name     = "${each.key}-policy"
  role     = aws_iam_role.irsa[each.key].id
  policy   = each.value.policy
}

# Backend API: S3 uploads + result reads, SQS send, DynamoDB job store CRUD
data "aws_iam_policy_document" "backend" {
  # Presigned multipart uploads are signed with this role, so a browser can write
  # exactly what this statement allows: the input bucket's uploads/ prefix, which
  # is the only prefix the API ever creates keys under.
  statement {
    actions = [
      "s3:PutObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${var.input_bucket_arn}/uploads/*"]
  }
  # Results are read back to the browser; the API never writes them.
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${var.output_bucket_arn}/results/*", "${var.tiles_bucket_arn}/tiles/*"]
  }
  # Kept so a missing result is NoSuchKey (404 to the browser, which the viewer
  # relies on for optional segmentation) rather than AccessDenied (500).
  statement {
    actions   = ["s3:ListBucket"]
    resources = [var.output_bucket_arn, var.tiles_bucket_arn]
  }
  statement {
    actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
    resources = [var.queue_arn]
  }
  # Job store: full CRUD (create on submit, get/scan for the dashboard, update on
  # status, delete when a job's data is erased).
  statement {
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
    ]
    resources = [var.jobs_table_arn]
  }
  # Erasing a job removes its upload, results and tiles, under the same three prefixes
  # the API already reads and writes and nowhere else. DeleteObject without a version
  # ID only adds a delete marker in these versioned buckets; the old version expires
  # through the buckets' noncurrent-version lifecycle rule. s3:DeleteObjectVersion,
  # which would destroy history immediately, is deliberately not granted.
  statement {
    actions = ["s3:DeleteObject"]
    resources = [
      "${var.input_bucket_arn}/uploads/*",
      "${var.output_bucket_arn}/results/*",
      "${var.tiles_bucket_arn}/tiles/*",
    ]
  }
  # @secure_recommendation: use of the S3 (SSE-KMS objects), SQS (encrypted queue) and
  # DynamoDB (encrypted table) CMKs.
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.s3_kms_key_arn, var.sqs_kms_key_arn, var.dynamodb_kms_key_arn]
  }
}

# Worker: S3 read input + write results/tiles, SQS receive/delete
data "aws_iam_policy_document" "worker" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${var.input_bucket_arn}/uploads/*"]
  }
  # Scoped to the two prefixes the worker writes. Per-job scoping is not expressible
  # here -- the job id is only known at runtime -- so a compromised worker can still
  # overwrite another job's results; this stops it writing anywhere else.
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${var.output_bucket_arn}/results/*", "${var.tiles_bucket_arn}/tiles/*"]
  }
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
    resources = [var.queue_arn]
  }
  # cloudwatch:PutMetricData on "*" used to be granted here. Nothing in the worker
  # publishes metrics, so it was unused privilege and has been removed rather than
  # scoped; add it back with a cloudwatch:namespace condition if that changes.
  #
  # Job store: worker updates job status directly (get + update). This replaces the
  # fragile HTTP status callback to the API, which broke whenever the request hit an
  # API replica that did not hold the job in its process-local store.
  statement {
    actions = [
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:PutItem",
    ]
    resources = [var.jobs_table_arn]
  }
  # @secure_recommendation: use of the S3 (SSE-KMS objects), SQS (encrypted queue) and
  # DynamoDB (encrypted table) CMKs.
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.s3_kms_key_arn, var.sqs_kms_key_arn, var.dynamodb_kms_key_arn]
  }
}

# No frontend role. The frontend is nginx serving static files and makes no AWS
# calls -- presigned upload URLs are signed by the backend role above -- so the S3
# write it used to hold was privilege nothing exercised.

# Training: S3 read data + read/write models
data "aws_iam_policy_document" "training" {
  statement {
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = ["${var.input_bucket_arn}/*", var.input_bucket_arn, "${var.models_bucket_arn}/*", var.models_bucket_arn]
  }
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${var.models_bucket_arn}/*"]
  }
  # @secure_recommendation: read/write of SSE-KMS objects requires use of the S3 CMK.
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.s3_kms_key_arn]
  }
}
