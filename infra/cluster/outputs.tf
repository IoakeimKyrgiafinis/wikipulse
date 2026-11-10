output "kube_context" {
  description = "kubectl context of the cluster (kubectl --context ...)."
  value       = kind_cluster.this.context_name
}

output "api_server" {
  description = "Address of the cluster's Kubernetes API server."
  value       = kind_cluster.this.endpoint
}

output "namespace" {
  description = "Namespace of the release."
  value       = kubernetes_namespace_v1.wikipulse.metadata[0].name
}

output "release" {
  description = "The Helm release: chart version, revision and status."
  value = {
    chart    = "${helm_release.wikipulse.metadata.chart}-${helm_release.wikipulse.metadata.version}"
    revision = helm_release.wikipulse.metadata.revision
    status   = helm_release.wikipulse.status
    values   = [for path in local.values_files : basename(path)]
  }
}

output "api_url" {
  description = "Where the API and the live page answer after the port-forward command below."
  value       = "http://localhost:8000"
}

output "api_port_forward" {
  description = "Run this to reach the API from the host."
  value       = "kubectl --context ${kind_cluster.this.context_name} -n ${kubernetes_namespace_v1.wikipulse.metadata[0].name} port-forward svc/api 8000:8000"
}
