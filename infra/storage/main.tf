provider "minio" {
  minio_server   = var.s3_server
  minio_region   = var.s3_region
  minio_user     = var.s3_access_key
  minio_password = var.s3_secret_key
  minio_ssl      = false
}

resource "minio_s3_bucket" "this" {
  for_each = var.buckets

  bucket = each.key
  # Never delete a bucket that still holds objects: the lake and the models are data,
  # not infrastructure that can be rebuilt.
  force_destroy = false

  lifecycle {
    # `terraform destroy` of this stack fails instead of deleting the buckets. To really
    # delete one, remove this line first, on purpose.
    prevent_destroy = true
  }
}
