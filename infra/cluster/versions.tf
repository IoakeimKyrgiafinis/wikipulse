# The cluster stack: the kind cluster, the namespace, the Secret, the image in the node and
# the Helm release of charts/wikipulse. Destroy and rebuild it freely: the data stays in
# Docker Compose and in the storage stack (../storage). See docs/learning/08c-terraform.md.
terraform {
  required_version = ">= 1.9"

  required_providers {
    # Community provider: creates and deletes kind clusters (Kubernetes in Docker).
    kind = {
      source  = "tehcyx/kind"
      version = "~> 0.13"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.5"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.3"
    }
  }

  # No backend block: the state is a local file, terraform.tfstate (git-ignored).
}
