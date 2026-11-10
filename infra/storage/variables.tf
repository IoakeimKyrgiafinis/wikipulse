variable "s3_server" {
  description = "Host and port of the S3 store, without a scheme (Compose publishes RustFS on 9000)."
  type        = string
  default     = "localhost:9000"
}

variable "s3_region" {
  description = "Region the S3 store reports (RustFS, like MinIO, uses us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "s3_access_key" {
  description = "S3 access key (S3_ACCESS_KEY). No default: pass it as TF_VAR_s3_access_key."
  type        = string
  sensitive   = true
}

variable "s3_secret_key" {
  description = "S3 secret key (S3_SECRET_KEY). No default: pass it as TF_VAR_s3_secret_key."
  type        = string
  sensitive   = true
}

variable "buckets" {
  description = "Buckets to manage: the data lake (S3_BUCKET) and MLflow's artifacts (MLFLOW_BUCKET)."
  type        = set(string)
  default     = ["wikipulse-lake", "wikipulse-mlflow"]
}
