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

output "grafana" {
  description = "Grafana: the port-forward command, the address, and how to read the admin password."
  value = var.monitoring ? {
    port_forward  = "kubectl --context ${kind_cluster.this.context_name} -n ${var.monitoring_namespace} port-forward svc/kube-prometheus-stack-grafana 3000:80"
    url           = "http://localhost:3000 (user admin)"
    read_admin_pw = "kubectl --context ${kind_cluster.this.context_name} -n ${var.monitoring_namespace} get secret kube-prometheus-stack-grafana -o jsonpath={.data.admin-password} | base64 -d"
  } : null
}

output "loki" {
  description = "Loki: query it in Grafana (Explore, data source Loki, or the dashboard WikiPulse: Event trail), or port-forward it."
  value = var.monitoring && var.logging ? {
    port_forward = "kubectl --context ${kind_cluster.this.context_name} -n ${var.monitoring_namespace} port-forward svc/loki 3100:3100"
    example      = "{system=\"wikipulse\"} | correlation_id=\"<id>\""
  } : null
}

output "prometheus" {
  description = "Prometheus: the port-forward command and the address (Alerts, Graph, Targets)."
  value = var.monitoring ? {
    port_forward = "kubectl --context ${kind_cluster.this.context_name} -n ${var.monitoring_namespace} port-forward svc/kube-prometheus-stack-prometheus 9090:9090"
    url          = "http://localhost:9090"
  } : null
}
