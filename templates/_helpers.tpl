{{/* Chart name (nameOverride-able). */}}
{{- define "silo.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Release-scoped full name. Avoids doubling when the release name
     already contains the chart name (installing as "silo" gives "silo"). */}}
{{- define "silo.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := include "silo.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* helm.sh/chart label. */}}
{{- define "silo.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels. */}}
{{- define "silo.labels" -}}
helm.sh/chart: {{ include "silo.chart" . }}
{{ include "silo.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Selector labels. */}}
{{- define "silo.selectorLabels" -}}
app.kubernetes.io/name: {{ include "silo.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Component labels; pass (dict "root" $ "component" "postgres"). */}}
{{- define "silo.componentLabels" -}}
{{ include "silo.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* ServiceAccount name. */}}
{{- define "silo.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "silo.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* storageClassName; pass (dict "root" $ "storageClass" <per-claim-or-nil>).
     Per-claim value wins over the top-level one; an empty result omits
     storageClassName so the cluster default provisioner is used. */}}
{{- define "silo.storageClass" -}}
{{- $root := index . "root" -}}
{{- $sc := index . "storageClass" -}}
{{- default $root.Values.storageClass $sc -}}
{{- end -}}

{{/* PostgreSQL password resolution:
     1. postgres.auth.existingSecret  -> looked up at render time
     2. postgres.auth.password        -> explicit value
     3. otherwise                     -> random password generated on first
        install and kept stable by re-reading the chart-managed secret. */}}
{{- define "silo.postgresPassword" -}}
{{- $auth := .Values.postgres.auth -}}
{{- if $auth.existingSecret -}}
{{- $secret := lookup "v1" "Secret" .Release.Namespace $auth.existingSecret -}}
{{- if $secret -}}
{{- index $secret.data $auth.existingSecretPasswordKey | b64dec -}}
{{- else -}}
{{- fail (printf "postgres.auth.existingSecret %q not found in namespace %q (helm template/--dry-run cannot read cluster secrets: install against the cluster, or set postgres.auth.password for offline rendering)" $auth.existingSecret .Release.Namespace) -}}
{{- end -}}
{{- else if $auth.password -}}
{{- $auth.password -}}
{{- else -}}
{{- $secret := lookup "v1" "Secret" .Release.Namespace (printf "%s-postgres-auth" (include "silo.fullname" .)) -}}
{{- if $secret -}}
{{- index $secret.data "postgres-password" | b64dec -}}
{{- else -}}
{{- randAlphaNum 24 -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* In-cluster DATABASE_URL (password is URL-escaped). */}}
{{- define "silo.databaseUrl" -}}
{{- $auth := .Values.postgres.auth -}}
{{- printf "postgres://%s:%s@%s-postgres:5432/%s?sslmode=disable" $auth.username (include "silo.postgresPassword" . | urlquery) (include "silo.fullname" .) $auth.database -}}
{{- end -}}

{{/* Render-time guards for values combinations that would produce a
     broken or non-starting release. Mirrors the compose file's own
     fail-fast checks (SECRET_KEY, MEILI_MASTER_KEY). */}}
{{- define "silo.validateValues" -}}
{{- if ne (int .Values.silo.replicaCount) 1 -}}
{{- fail "silo.replicaCount must be 1: Silo is a single-instance server backed by ReadWriteOnce volumes" -}}
{{- end -}}
{{- if and (not .Values.silo.existingSecret) (not .Values.silo.secretKey) -}}
{{- fail "Silo refuses to start without SECRET_KEY. Generate one with: openssl rand -base64 48 ; store it in a Secret (kubectl create secret generic silo-secrets --from-literal=SECRET_KEY='<generated key>') and set silo.existingSecret, or set silo.secretKey directly (convenient for trials; stored in the release)" -}}
{{- end -}}
{{- if and (not .Values.postgres.enabled) (not .Values.silo.databaseUrlOverride) -}}
{{- fail "silo.databaseUrlOverride is required when postgres.enabled=false (e.g. point it at a CloudNativePG cluster)" -}}
{{- end -}}
{{- if and (not .Values.redis.enabled) (not .Values.silo.redisUrlOverride) -}}
{{- fail "silo.redisUrlOverride is required when redis.enabled=false" -}}
{{- end -}}
{{- if and .Values.meilisearch.enabled (not .Values.meilisearch.masterKey) (not .Values.meilisearch.existingSecret) -}}
{{- fail "meilisearch.enabled=true requires meilisearch.masterKey or meilisearch.existingSecret (mirrors the compose entrypoint check)" -}}
{{- end -}}
{{- if .Values.gateway.enabled -}}
{{- if not .Values.gateway.hostnames -}}
{{- fail "gateway.enabled=true requires gateway.hostnames (e.g. [silo.example.com])" -}}
{{- end -}}
{{- if and (not .Values.gateway.parentRefs) (not .Values.gateway.gateway.create) -}}
{{- fail "gateway.enabled=true needs somewhere to attach: set gateway.gateway.create=true to create a chart-managed Gateway, or provide gateway.parentRefs pointing at an existing one" -}}
{{- end -}}
{{- if and .Values.gateway.gateway.create (not .Values.gateway.gateway.gatewayClassName) -}}
{{- fail "gateway.gateway.create=true requires gateway.gateway.gatewayClassName (must reference an installed GatewayClass, e.g. envoy, cilium, nginx)" -}}
{{- end -}}
{{- if and .Values.gateway.jellyfin.enabled (not .Values.gateway.jellyfin.hostnames) -}}
{{- fail "gateway.jellyfin.enabled=true requires gateway.jellyfin.hostnames (the client API has no useful default host)" -}}
{{- end -}}
{{- if and .Values.gateway.audiobookshelf.enabled (not .Values.gateway.audiobookshelf.hostnames) -}}
{{- fail "gateway.audiobookshelf.enabled=true requires gateway.audiobookshelf.hostnames" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Chart-managed Gateway name. */}}
{{- define "silo.gatewayName" -}}
{{- printf "%s-gateway" (include "silo.fullname" .) -}}
{{- end -}}

{{/* parentRefs body for the HTTPRoutes (caller indents it). Uses the user's
     list when provided, otherwise the chart-managed Gateway. */}}
{{- define "silo.gatewayParentRefs" -}}
{{- if .Values.gateway.parentRefs -}}
{{- toYaml .Values.gateway.parentRefs -}}
{{- else -}}
{{- printf "- name: %s" (include "silo.gatewayName" .) -}}
{{- end -}}
{{- end -}}
