# --- logs (spec 09b) ---
#
# Loki stores the logs, Alloy collects them, Grafana searches them (its Loki data source
# is in monitoring.tf). Both charts are the community ones from grafana/helm-charts, in the
# monitoring namespace, so `-var monitoring=false` turns them off too. Turn off only the
# logs with `-var logging=false` (about 300 MB less in the kind node).
#
#   pods in var.namespace --stdout/stderr--> kubelet --API (pods/log)--> Alloy --push--> Loki
#                                                                    Grafana --LogQL--> Loki

locals {
  logging  = var.monitoring && var.logging
  loki_url = "http://loki.${var.monitoring_namespace}.svc.cluster.local:3100"
}

resource "helm_release" "loki" {
  count = local.logging ? 1 : 0

  name       = "loki"
  repository = "https://grafana.github.io/helm-charts"
  chart      = "loki"
  version    = var.loki_chart_version
  namespace  = kubernetes_namespace_v1.monitoring[0].metadata[0].name

  values = [file("${local.observability}/loki.yaml")]

  wait        = true
  timeout     = var.helm_timeout
  max_history = 5
}

resource "helm_release" "alloy" {
  count = local.logging ? 1 : 0

  name       = "alloy"
  repository = "https://grafana.github.io/helm-charts"
  chart      = "alloy"
  version    = var.alloy_chart_version
  namespace  = kubernetes_namespace_v1.monitoring[0].metadata[0].name

  values = [
    file("${local.observability}/alloy.yaml"),
    yamlencode({
      alloy = {
        configMap = {
          content = templatefile("${local.observability}/alloy/config.alloy.tftpl", {
            namespace = var.namespace
            loki_url  = local.loki_url
          })
        }
      }
    }),
  ]

  wait        = true
  timeout     = var.helm_timeout
  max_history = 5

  depends_on = [helm_release.loki]
}
