# The storage stack: the buckets of the S3 store (RustFS in Docker Compose).
# Its own state, apart from the cluster stack (../cluster): destroying and rebuilding the
# cluster must never touch the data lake. See docs/learning/08c-terraform.md.
terraform {
  required_version = ">= 1.9"

  required_providers {
    # A community provider for MinIO's S3 and admin APIs. RustFS speaks the same S3 API;
    # only buckets are managed here, which is plain S3.
    minio = {
      source  = "aminueza/minio"
      version = "~> 3.46"
    }
  }

  # No backend block: the state is a local file, terraform.tfstate (git-ignored).
}
