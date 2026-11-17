# Job store table. Replaces the process-local in-memory JobStore so job state is
# shared across all API replicas and survives pod restarts. Without this, the API
# running >1 replica returns inconsistent job listings (the dashboard table flickers)
# and the worker's status callback lands on a replica that does not know the job, so
# jobs appear stuck in "processing".
#
# Access pattern is purely key/value by job_id plus a full-table scan for the job
# list (the UI lists at most a few hundred jobs), so a single partition key and
# on-demand (PAY_PER_REQUEST) billing fit without provisioned capacity planning.

# @secure_recommendation: encrypt at rest with a customer-managed KMS key rather than the
# AWS-owned default, so encryption is auditable via CloudTrail and governed by our IAM.
resource "aws_dynamodb_table" "jobs" {
  name         = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  point_in_time_recovery {
    enabled = var.point_in_time_recovery
  }

  # Job records expire so the table does not grow without bound. This matters more
  # than usual here because the job list is served by a full table Scan (see above),
  # so every listing pays for every job ever submitted. The application sets this
  # attribute to creation time plus JOBS_TTL_DAYS and carries the same value forward
  # on later updates, so retention runs from creation rather than from the last write.
  # DynamoDB deletes expired items in the background at no request cost. Items written
  # without the attribute never expire, so enabling this is safe for existing rows;
  # they pick up an expiry the next time the application updates them.
  ttl {
    attribute_name = "expires_at"
    enabled        = var.ttl_enabled
  }

  tags = {
    Name = var.table_name
  }
}
