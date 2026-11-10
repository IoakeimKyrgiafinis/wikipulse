# --- the environment ---

# Changing cluster_name, node_image or kubeconfig_path REPLACES the cluster, and the plan
# then fails ("localhost"): destroy with `-refresh=false` first, then apply (infra/README.md).

variable "cluster_name" {
  description = "Name of the kind cluster; kubectl's context becomes kind-<name>. Changing it replaces the cluster: destroy first (infra/README.md)."
  type        = string
  default     = "wikipulse"
}

variable "node_image" {
  description = "kindest/node image (the Kubernetes version). null: kind's default. Changing it replaces the cluster: destroy first (infra/README.md)."
  type        = string
  default     = null
}

variable "kubeconfig_path" {
  description = "The kubeconfig kind merges the cluster's context into (kubectl's default). Stored expanded (an absolute path of this user): changing it, or using the state as another user, replaces the cluster."
  type        = string
  default     = "~/.kube/config"
}

variable "namespace" {
  description = "Namespace of the release. One release per namespace (charts/wikipulse/values.yaml)."
  type        = string
  default     = "wikipulse"
}

variable "environment" {
  description = "Which values file of the chart to use: dev (the laptop) or prod."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be \"dev\" or \"prod\" (charts/wikipulse/values-<environment>.yaml)."
  }
}

variable "offline" {
  description = <<-EOT
    true: add values-offline.yaml, so the services that call Wikimedia (consumer, enricher,
    graph) run with 0 replicas. Required while user_agent holds the placeholder contact.
  EOT
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Tag of the wikipulse image to load and deploy, built first by `scripts/cluster.py build`."
  type        = string
}

variable "chart_checksum" {
  description = "Checksum of charts/wikipulse's files, set by scripts/infra.py. A new value makes apply run `helm upgrade`, so template edits are deployed without a chart version bump."
  type        = string
}

variable "helm_timeout" {
  description = "Seconds Helm waits for the migration hook and the pods (the first grouper start downloads the embedding model)."
  type        = number
  default     = 900
}

# --- secrets: no defaults, never in a .tf file. Pass them as TF_VAR_<name> environment
# variables (scripts/infra.py does it from your settings) or in a git-ignored .tfvars file.

variable "postgres_dsn" {
  description = "Postgres DSN as the pods use it: host `postgres`, the in-cluster Service name."
  type        = string
  sensitive   = true
}

variable "neo4j_password" {
  description = "Neo4j password (NEO4J_PASSWORD)."
  type        = string
  sensitive   = true
}

variable "s3_access_key" {
  description = "S3 access key (S3_ACCESS_KEY)."
  type        = string
  sensitive   = true
}

variable "s3_secret_key" {
  description = "S3 secret key (S3_SECRET_KEY)."
  type        = string
  sensitive   = true
}

variable "user_agent" {
  description = "User-Agent for Wikimedia, with your contact address (USER_AGENT)."
  type        = string
  sensitive   = true

  # The same rule as scripts/cluster.py: Wikimedia requires a real contact, so a
  # placeholder is only allowed when nothing in the cluster calls Wikimedia.
  validation {
    condition = var.offline || !anytrue([
      for marker in ["<you>", "<your email>", "example.com"] : strcontains(var.user_agent, marker)
    ])
    error_message = "user_agent still holds the placeholder contact. Set USER_AGENT in .env to your own address, or deploy with offline = true (no pod then calls Wikimedia)."
  }
}
