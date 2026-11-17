output "input_bucket_arn" { value = aws_s3_bucket.buckets["input"].arn }
output "output_bucket_arn" { value = aws_s3_bucket.buckets["output"].arn }
output "tiles_bucket_arn" { value = aws_s3_bucket.buckets["tiles"].arn }
output "models_bucket_arn" { value = aws_s3_bucket.buckets["models"].arn }

output "input_bucket_name" { value = aws_s3_bucket.buckets["input"].id }
output "output_bucket_name" { value = aws_s3_bucket.buckets["output"].id }
output "tiles_bucket_name" { value = aws_s3_bucket.buckets["tiles"].id }
output "models_bucket_name" { value = aws_s3_bucket.buckets["models"].id }

output "bucket_arns" {
  value = { for k, v in aws_s3_bucket.buckets : k => v.arn }
}
