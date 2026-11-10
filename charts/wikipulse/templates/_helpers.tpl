{{/*
Named templates shared by the chart's files. `include "name" <argument>` renders one.
Templates that need both the chart (`.Values`, `.Release`) and a service name take a
dict: (dict "root" $ "name" "consumer").
*/}}

{{/* chart label value: name-version */}}
{{- define "wikipulse.chart" -}}
{{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end }}

{{/*
Labels for every object. `app.kubernetes.io/name` is the service (consumer, api, ...);
the network policies and scripts/k8s_crash_test.py select pods by it.
Argument: (dict "root" $ "name" <service>)
*/}}
{{- define "wikipulse.labels" -}}
app.kubernetes.io/part-of: wikipulse
{{- with .name }}
app.kubernetes.io/name: {{ . }}
{{- end }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ include "wikipulse.chart" .root }}
{{- end }}

{{/*
Selector labels: the few labels that pick a Deployment's pods. A Deployment's selector
can never change, so it holds only labels that never change either (not the version).
*/}}
{{- define "wikipulse.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end }}

{{- define "wikipulse.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end }}

{{/*
Pod-level security, the same for every pod. The namespace enforces the "restricted"
Pod Security Standard, so a pod without these settings would be refused anyway.
*/}}
{{- define "wikipulse.podSecurity" -}}
automountServiceAccountToken: false  # no pod talks to the Kubernetes API
# No "service links": Kubernetes would otherwise set variables such as
# API_PORT=tcp://10.96.0.12:8000 for every Service, and API_PORT is a WikiPulse setting.
enableServiceLinks: false
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile:
    type: RuntimeDefault
{{- end }}

{{- define "wikipulse.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: [ALL]
{{- end }}

{{/*
The worker probes: `python -m wikipulse.health --max-age N` exits 1 when the heartbeat
file is older than N seconds. Argument: the service's values.
*/}}
{{- define "wikipulse.heartbeatProbes" -}}
startupProbe:
  exec:
    command: [python, -m, wikipulse.health, --max-age, {{ .heartbeat.readyMaxAge | quote }}]
  periodSeconds: {{ .startup.periodSeconds }}
  timeoutSeconds: 10
  failureThreshold: {{ .startup.failureThreshold }}
readinessProbe:
  exec:
    command: [python, -m, wikipulse.health, --max-age, {{ .heartbeat.readyMaxAge | quote }}]
  periodSeconds: 15
  timeoutSeconds: 10
livenessProbe:
  exec:
    command: [python, -m, wikipulse.health, --max-age, {{ .heartbeat.liveMaxAge | quote }}]
  periodSeconds: 30
  timeoutSeconds: 10
  failureThreshold: 3
{{- end }}

{{/*
The API's probes: GET /health. The Host header must be a loopback name: the API refuses
others (protection against DNS rebinding).
*/}}
{{- define "wikipulse.httpProbes" -}}
startupProbe:
  httpGet: {path: /health, port: http, httpHeaders: [{name: Host, value: localhost}]}
  periodSeconds: 5
  failureThreshold: 24
readinessProbe:
  httpGet: {path: /health, port: http, httpHeaders: [{name: Host, value: localhost}]}
  periodSeconds: 10
  timeoutSeconds: 5
livenessProbe:
  httpGet: {path: /health, port: http, httpHeaders: [{name: Host, value: localhost}]}
  periodSeconds: 30
  timeoutSeconds: 10
  failureThreshold: 3
{{- end }}
