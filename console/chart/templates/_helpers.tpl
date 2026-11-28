{{- define "console.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "console.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "console.selectorLabels" -}}
app.kubernetes.io/name: {{ include "console.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "console.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "console.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "console.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "console.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "console.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}

{{/* Refuse settings the console would refuse, before anything is installed. */}}
{{- define "console.validate" -}}
{{- if not .Values.database.existingSecret -}}
{{- fail "database.existingSecret is required: a Secret holding the Postgres URL" -}}
{{- end -}}
{{- $mode := .Values.auth.mode -}}
{{- if not (has $mode (list "oidc" "local" "none")) -}}
{{- fail (printf "auth.mode must be oidc, local or none, not %s" $mode) -}}
{{- end -}}
{{- if and (eq $mode "none") (not .Values.auth.allowNone) -}}
{{- fail "auth.mode none lets everyone who reaches the console act as an admin; set auth.allowNone=true for a development install" -}}
{{- end -}}
{{- if and (ne $mode "none") (not .Values.auth.secretKey.existingSecret) -}}
{{- fail "auth.secretKey.existingSecret is required to sign session cookies" -}}
{{- end -}}
{{- if eq $mode "oidc" -}}
{{- if not .Values.auth.publicUrl -}}
{{- fail "auth.publicUrl is required with oidc, for the redirect" -}}
{{- end -}}
{{- if or (not .Values.auth.oidc.issuer) (not .Values.auth.oidc.clientId) -}}
{{- fail "auth.oidc.issuer and auth.oidc.clientId are required with oidc" -}}
{{- end -}}
{{- end -}}
{{- if not (has .Values.reconcile.mode (list "inPod" "cronJob")) -}}
{{- fail "reconcile.mode must be inPod or cronJob" -}}
{{- end -}}
{{- if not (has (include "console.metricsMode" .) (list "off" "token" "port")) -}}
{{- fail "metrics.mode must be off, token or port" -}}
{{- end -}}
{{- if and .Values.events.enabled (not .Values.events.queueUrl) -}}
{{- fail "events.queueUrl is required when events are enabled" -}}
{{- end -}}
{{- if and .Values.metrics.serviceMonitor.enabled (eq (include "console.metricsMode" .) "off") -}}
{{- fail "metrics.serviceMonitor needs metrics.mode token or port" -}}
{{- end -}}
{{- if and .Values.metrics.serviceMonitor.enabled (eq (include "console.metricsMode" .) "token") (not .Values.metrics.serviceMonitor.tokenSecret.name) -}}
{{- fail "metrics.serviceMonitor.tokenSecret.name is required in token mode" -}}
{{- end -}}
{{- end -}}

{{- define "console.configMapName" -}}
{{- include "console.fullname" . -}}-config
{{- end -}}

{{/* The config files and where each comes from: [file, inline values, existing ConfigMap]. */}}
{{- define "console.files" -}}
{{- $files := list -}}
{{- if or .Values.stores .Values.storesConfigMap -}}
{{- $files = append $files (list "stores.yaml" .Values.storesConfigMap "STORES_FILE") -}}
{{- end -}}
{{- if or .Values.groups .Values.groupsConfigMap -}}
{{- $files = append $files (list "groups.yaml" .Values.groupsConfigMap "GROUPS_FILE") -}}
{{- end -}}
{{- if or .Values.auth.roles .Values.auth.rolesConfigMap -}}
{{- $files = append $files (list "roles.yaml" .Values.auth.rolesConfigMap "ROLES_FILE") -}}
{{- end -}}
{{- toJson $files -}}
{{- end -}}

{{/* Settings every console container shares: the web app, the CronJob and the migration Job. */}}
{{- define "console.commonEnv" -}}
- name: TFWORKER_CONSOLE_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.key }}
{{- range (include "console.files" . | fromJsonArray) }}
- name: TFWORKER_CONSOLE_{{ index . 2 }}
  value: /config/{{ index . 0 }}/{{ index . 0 }}
{{- end }}
{{- if .Values.awsProfiles.existingSecret }}
- name: AWS_CONFIG_FILE
  value: /aws/config
- name: AWS_SHARED_CREDENTIALS_FILE
  value: /aws/credentials
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "console.volumes" -}}
- name: tmp
  emptyDir: {}
{{- range (include "console.files" . | fromJsonArray) }}
- name: {{ index . 0 | replace ".yaml" "" }}
  configMap:
    name: {{ default (include "console.configMapName" $) (index . 1) }}
    items:
      - key: {{ index . 0 }}
        path: {{ index . 0 }}
{{- end }}
{{- if .Values.awsProfiles.existingSecret }}
- name: aws
  secret:
    secretName: {{ .Values.awsProfiles.existingSecret }}
    items:
      - key: {{ .Values.awsProfiles.configKey }}
        path: config
      - key: {{ .Values.awsProfiles.credentialsKey }}
        path: credentials
{{- end }}
{{- end -}}

{{- define "console.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
{{- range (include "console.files" . | fromJsonArray) }}
- name: {{ index . 0 | replace ".yaml" "" }}
  mountPath: /config/{{ index . 0 }}
  readOnly: true
{{- end }}
{{- if .Values.awsProfiles.existingSecret }}
- name: aws
  mountPath: /aws
  readOnly: true
{{- end }}
{{- end -}}

{{- define "console.podSpecCommon" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* metrics.mode, where an unquoted off reads as false in YAML */}}
{{- define "console.metricsMode" -}}
{{- $mode := .Values.metrics.mode -}}
{{- if eq (toString $mode) "false" -}}off{{- else -}}{{ toString $mode }}{{- end -}}
{{- end -}}
