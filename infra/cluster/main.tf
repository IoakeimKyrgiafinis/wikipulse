locals {
  chart = "${path.module}/../../charts/wikipulse"
  # Merged in this order, as by scripts/cluster.py: values.yaml (implicit), the
  # environment's file, then the offline overlay.
  values_files = concat(
    ["${local.chart}/values-${var.environment}.yaml"],
    var.offline ? ["${local.chart}/values-offline.yaml"] : [],
  )
  labels = { "app.kubernetes.io/part-of" = "wikipulse" }
  # The Secret's values, also hashed into the release below (see there).
  secret_data = {
    POSTGRES_DSN   = var.postgres_dsn
    NEO4J_PASSWORD = var.neo4j_password
    S3_ACCESS_KEY  = var.s3_access_key
    S3_SECRET_KEY  = var.s3_secret_key
    USER_AGENT     = var.user_agent
  }
}

# --- the cluster ---

# One node, control plane and worker at once (as k8s/kind-config.yaml, which
# scripts/cluster.py uses). kind adds the context kind-<name> to your kubeconfig (and
# removes it on destroy), so kubectl and scripts/cluster.py work on this cluster too.
resource "kind_cluster" "this" {
  name           = var.cluster_name
  node_image     = var.node_image
  wait_for_ready = true
  # Without a path the provider writes a second kubeconfig, with the cluster's client key,
  # into this folder (<name>-config).
  kubeconfig_path = pathexpand(var.kubeconfig_path)

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"
    }
  }
}

# The Kubernetes and Helm providers talk to the cluster created above, with the
# certificates kind generated. Terraform creates the cluster first, then configures them.
# The catch: a change that *replaces* the cluster (cluster_name, node_image,
# kubeconfig_path, kind_config) leaves these values unknown at plan time, and the plan
# fails reading the namespace from "localhost". Destroy first, then apply: the destroy
# with `-refresh=false`, so it doesn't try to plan the replacement.
provider "kubernetes" {
  host                   = kind_cluster.this.endpoint
  client_certificate     = kind_cluster.this.client_certificate
  client_key             = kind_cluster.this.client_key
  cluster_ca_certificate = kind_cluster.this.cluster_ca_certificate
}

provider "helm" {
  kubernetes = {
    host                   = kind_cluster.this.endpoint
    client_certificate     = kind_cluster.this.client_certificate
    client_key             = kind_cluster.this.client_key
    cluster_ca_certificate = kind_cluster.this.cluster_ca_certificate
  }
}

# --- the image ---

# kind has no registry: the image is copied into the node. Building it is not
# Terraform's job (scripts/cluster.py build); this only loads the tag it is given, again
# whenever the tag or the cluster changes. `kind load` runs the same on Windows and Linux.
resource "terraform_data" "image" {
  triggers_replace = [kind_cluster.this.id, var.image_tag]

  provisioner "local-exec" {
    command = "kind load docker-image wikipulse:${var.image_tag} --name ${kind_cluster.this.name}"
  }
}

# --- the namespace and the Secret ---

# Outside the chart, as in 08b: Helm keeps its release records in the namespace, and
# Pod Security Admission refuses pods that could run as root.
resource "kubernetes_namespace_v1" "wikipulse" {
  metadata {
    name = var.namespace
    labels = merge(local.labels, {
      "pod-security.kubernetes.io/enforce"         = "restricted"
      "pod-security.kubernetes.io/enforce-version" = "latest"
    })
  }
}

# The pods read it by name (existingSecret in the chart). The values come from
# variables only; Terraform keeps them in its state, which is why the state is
# git-ignored and must be treated as a secret itself.
resource "kubernetes_secret_v1" "wikipulse" {
  metadata {
    name      = "wikipulse-secrets"
    namespace = kubernetes_namespace_v1.wikipulse.metadata[0].name
    labels    = local.labels
  }
  type = "Opaque"
  data = local.secret_data
}

# --- the release ---

# The same as `helm upgrade --install wikipulse charts/wikipulse -f values-<env>.yaml
# [-f values-offline.yaml] --set image.tag=<tag> --wait`. Helm runs the migration hook
# first and waits for the pods.
#
# The provider only compares the release's *inputs* (chart version, values), not the
# rendered templates, so two checksums make changes it can't see visible:
# - checksums.chart: of the chart's files (scripts/infra.py computes it). A template edit
#   without a chart version bump changes it, so `apply` runs `helm upgrade`.
# - checksums.secret: of the Secret's values. A new password updates the Secret, but pods
#   only read it at start; as a pod annotation it makes the Deployments roll.
resource "helm_release" "wikipulse" {
  name      = "wikipulse"
  chart     = local.chart
  namespace = kubernetes_namespace_v1.wikipulse.metadata[0].name

  values = [for path in local.values_files : file(path)]
  set = [
    { name = "image.tag", value = var.image_tag },
    { name = "checksums.chart", value = var.chart_checksum },
  ]
  # A hash of secrets is still kept out of the plan output.
  set_sensitive = [
    { name = "checksums.secret", value = sha256(jsonencode(local.secret_data)) },
  ]

  wait          = true
  wait_for_jobs = true
  timeout       = var.helm_timeout
  max_history   = 10

  depends_on = [kubernetes_secret_v1.wikipulse, terraform_data.image]
}
