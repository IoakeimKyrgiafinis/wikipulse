output "s3_endpoint" {
  description = "The S3 endpoint as the host sees it (S3_ENDPOINT)."
  value       = "http://${var.s3_server}"
}

output "buckets" {
  description = "The managed buckets."
  value       = sort([for bucket in minio_s3_bucket.this : bucket.bucket])
}
