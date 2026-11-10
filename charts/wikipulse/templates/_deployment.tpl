{{/*
One WikiPulse Deployment. Every service runs the same image with its own command and
settings from values.yaml (.Values.<name>). Argument:
  (dict "root" $ "name" "consumer")            a worker: a singleton with heartbeat probes
  (dict "root" $ "name" "api" "http" true)     the API: rolling updates, HTTP port and probes
*/}}
{{- define "wikipulse.deployment" -}}
{{- $root := .root -}}
{{- $values := index $root.Values .name -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .name }}
  labels:
    {{- include "wikipulse.labels" . | nindent 4 }}
spec:
  replicas: {{ $values.replicas }}
  strategy:
    {{- if .http }}
    # Stateless: the new pod starts and must be ready before an old one stops.
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
    {{- else }}
    # A singleton: the old pod stops before the new one starts, so there are never two.
    type: Recreate
    {{- end }}
  selector:
    matchLabels:
      {{- include "wikipulse.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "wikipulse.labels" . | nindent 8 }}
      annotations:
        # A new ConfigMap means new pods: environment variables are only read at start.
        checksum/config: {{ include (print $root.Template.BasePath "/configmap.yaml") $root | sha256sum }}
        {{- with ($root.Values.checksums).secret }}
        # A new Secret value means new pods too (only known when Terraform deploys).
        checksum/secret: {{ . | quote }}
        {{- end }}
    spec:
      {{- include "wikipulse.podSecurity" . | nindent 6 }}
      terminationGracePeriodSeconds: {{ $values.terminationGracePeriodSeconds }}
      containers:
        - name: {{ .name }}
          image: {{ include "wikipulse.image" $root }}
          imagePullPolicy: {{ $root.Values.image.pullPolicy }}
          command: {{ toJson $values.command }}
          {{- if .http }}
          env:
            # Listen on the pod's address, so the Service and the probes reach it.
            - {name: API_HOST, value: "0.0.0.0"}
            - {name: API_PORT, value: {{ $values.port | quote }}}
            - {name: API_LISTEN_ALL, value: "true"}
          {{- end }}
          envFrom:
            - configMapRef: {name: wikipulse-config}
            - secretRef: {name: {{ $root.Values.existingSecret }}}
          {{- if .http }}
          ports:
            - {name: http, containerPort: {{ $values.port }}}
          {{- end }}
          resources:
            {{- toYaml $values.resources | nindent 12 }}
          {{- include "wikipulse.containerSecurity" . | nindent 10 }}
          {{- if .http }}
          {{- include "wikipulse.httpProbes" $values | nindent 10 }}
          {{- else }}
          {{- include "wikipulse.heartbeatProbes" $values | nindent 10 }}
          {{- end }}
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
            {{- if $values.modelCache }}
            - {name: model-cache, mountPath: /models}
            {{- end }}
      volumes:
        - name: tmp
          emptyDir: {sizeLimit: 64Mi}
        {{- if $values.modelCache }}
        - name: model-cache
          persistentVolumeClaim: {claimName: model-cache}
        {{- end }}
{{- end }}
