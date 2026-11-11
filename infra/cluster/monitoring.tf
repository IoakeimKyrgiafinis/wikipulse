# --- monitoring (spec 09a) ---
#
# Prometheus and Grafana from the community chart kube-prometheus-stack, in their own
# namespace, with WikiPulse's alert rules and dashboards from observability/. Turn it off
# with `-var monitoring=false` (about 600 MB less in the kind node).
#
#   Prometheus  --PodMonitor "wikipulse"-->  every pod's port `metrics` (9400) in var.namespace
#               --static targets-------->  var.host_metrics_targets (e.g. a replay on the laptop)
#               --rules----------------->  observability/alerts/wikipulse.rules.yaml
#   Grafana     <--sidecar---------------  ConfigMaps from observability/dashboards/*.json

locals {
  observability = "${path.module}/../../observability"
  dashboards    = var.monitoring ? fileset("${local.observability}/dashboards", "*.json") : []

  # The values that depend on variables; the rest is in observability/kube-prometheus-stack.yaml.
  monitoring_values = {
    prometheus = {
      additionalPodMonitors = [{
        name              = "wikipulse"
        selector          = { matchLabels = { "app.kubernetes.io/part-of" = "wikipulse" } }
        namespaceSelector = { matchNames = [var.namespace] }
        podMetricsEndpoints = [{
          port     = "metrics"
          interval = "15s"
          relabelings = [
            # `service` = the pod's app.kubernetes.io/name (consumer, detector, api, ...).
            {
              sourceLabels = ["__meta_kubernetes_pod_label_app_kubernetes_io_name"]
              targetLabel  = "service"
            },
            # `system="wikipulse"` tells WikiPulse's processes from Prometheus' and Grafana's own.
            { targetLabel = "system", replacement = "wikipulse" },
          ]
        }]
      }]
      prometheusSpec = {
        additionalScrapeConfigs = [
          for name, target in var.host_metrics_targets : {
            job_name        = "wikipulse-host-${name}"
            scrape_interval = "15s"
            static_configs  = [{ targets = [target], labels = { service = name, system = "wikipulse" } }]
          }
        ]
      }
    }
    # Spec 09b: Loki next to Prometheus, by a fixed uid the log dashboard refers to.
    grafana = {
      additionalDataSources = local.logging ? [{
        name   = "Loki"
        type   = "loki"
        uid    = "loki"
        url    = local.loki_url
        access = "proxy"
        jsonData = {
          maxLines = 5000
        }
      }] : []
    }
    additionalPrometheusRulesMap = {
      wikipulse = yamldecode(file("${local.observability}/alerts/wikipulse.rules.yaml"))
    }
  }
}

resource "kubernetes_namespace_v1" "monitoring" {
  count = var.monitoring ? 1 : 0

  metadata {
    name = var.monitoring_namespace
    labels = merge(local.labels, {
      # "baseline", not "restricted": the chart's pods don't all declare every setting
      # the restricted profile insists on. None of them runs privileged.
      "pod-security.kubernetes.io/enforce"         = "baseline"
      "pod-security.kubernetes.io/enforce-version" = "latest"
    })
  }
}

resource "helm_release" "monitoring" {
  count = var.monitoring ? 1 : 0

  name       = "kube-prometheus-stack"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = var.kube_prometheus_stack_version
  namespace  = kubernetes_namespace_v1.monitoring[0].metadata[0].name

  values = [
    file("${local.observability}/kube-prometheus-stack.yaml"),
    yamlencode(local.monitoring_values),
  ]

  wait        = true
  timeout     = var.helm_timeout
  max_history = 5
}

# One ConfigMap per dashboard file. Grafana's sidecar watches for the label
# grafana_dashboard=1 and loads the JSON into the folder named by grafana_folder.
resource "kubernetes_config_map_v1" "dashboard" {
  for_each = toset(local.dashboards)

  metadata {
    name      = "dashboard-${trimsuffix(each.value, ".json")}"
    namespace = kubernetes_namespace_v1.monitoring[0].metadata[0].name
    labels    = merge(local.labels, { grafana_dashboard = "1" })
    annotations = {
      grafana_folder = "WikiPulse"
    }
  }

  data = {
    (each.value) = file("${local.observability}/dashboards/${each.value}")
  }
}
