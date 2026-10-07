{{- define "bank.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "bank.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains (include "bank.name" .) .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "bank.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "bank.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/name: {{ include "bank.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: uk-banking-agent-platform
{{- end -}}

{{- define "bank.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bank.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Name of one component's resources, e.g. "<release>-gateway". Call with (dict "root" $ "name" "gateway"). */}}
{{- define "bank.component" -}}
{{- printf "%s-%s" (include "bank.fullname" .root) .name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Pod selector for one component. Call with (dict "root" $ "name" "gateway"). */}}
{{- define "bank.componentSelector" -}}
{{ include "bank.selectorLabels" .root }}
app.kubernetes.io/component: {{ .name }}
{{- end -}}

{{- define "bank.secretName" -}}
{{- default (printf "%s-secrets" (include "bank.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}

{{- define "bank.redisUrl" -}}
{{- if .Values.redis.deploy -}}
{{- printf "%s://%s:6379/0" (ternary "rediss" "redis" .Values.tls.enabled) (include "bank.component" (dict "root" . "name" "redis")) -}}
{{- else -}}
{{- required "config.redisUrl is required when redis.deploy is false" .Values.config.redisUrl -}}
{{- end -}}
{{- end -}}

{{- define "bank.scheme" -}}{{ ternary "https" "http" .Values.tls.enabled }}{{- end -}}

{{- define "bank.tlsSecret" -}}
{{- printf "%s-tls" (include "bank.component" .) -}}
{{- end -}}

{{- define "bank.demoIdpUrl" -}}
{{- printf "%s://%s:%v" (include "bank.scheme" .) (include "bank.component" (dict "root" . "name" .Values.components.demoIdp.name)) .Values.components.demoIdp.port -}}
{{- end -}}

{{- define "bank.oidcIssuer" -}}
{{- if .Values.config.oidc.issuer -}}{{ .Values.config.oidc.issuer }}
{{- else if .Values.components.demoIdp.enabled -}}{{ include "bank.demoIdpUrl" . }}
{{- else -}}{{ required "config.oidc.issuer is required without the demo IdP" .Values.config.oidc.issuer }}
{{- end -}}
{{- end -}}

{{- define "bank.oidcJwksUrl" -}}
{{- if .Values.config.oidc.jwksUrl -}}{{ .Values.config.oidc.jwksUrl }}
{{- else if .Values.components.demoIdp.enabled -}}{{ printf "%s/.well-known/jwks.json" (include "bank.demoIdpUrl" .) }}
{{- else -}}{{ required "config.oidc.jwksUrl is required without the demo IdP" .Values.config.oidc.jwksUrl }}
{{- end -}}
{{- end -}}

{{- define "bank.image" -}}
{{- $img := ternary .root.Values.webImage .root.Values.image (eq .component.kind "web") -}}
{{- printf "%s:%s" $img.repository (default .root.Chart.AppVersion $img.tag) -}}
{{- end -}}

{{/* Egress rule to Redis: the in-cluster pod, or the managed Redis CIDR. */}}
{{- define "bank.np.redis" -}}
{{- if .Values.redis.deploy -}}
- to:
    - podSelector:
        matchLabels: {{- include "bank.componentSelector" (dict "root" . "name" "redis") | nindent 10 }}
  ports: [{port: 6379, protocol: TCP}]
{{- else -}}
- to: [{ipBlock: {cidr: {{ required "networkPolicy.externalRedis.cidr is required with a managed Redis" .Values.networkPolicy.externalRedis.cidr }}}}]
  ports: [{port: {{ .Values.networkPolicy.externalRedis.port }}, protocol: TCP}]
{{- end -}}
{{- end -}}

{{- define "bank.np.from" -}}
- podSelector:
    matchLabels: {{- include "bank.componentSelector" . | nindent 6 }}
{{- end -}}
{{- define "bank.np.to" -}}
- to:
    - podSelector:
        matchLabels: {{- include "bank.componentSelector" (dict "root" .root "name" .name) | nindent 10 }}
  ports: [{port: {{ .port }}, protocol: TCP}]
{{- end -}}
