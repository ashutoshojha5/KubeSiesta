{{- define "kube-siesta.fullname" -}}
{{- if contains .Chart.Name .Release.Name }}{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "kube-siesta.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{/* Kept to `app: <fullname>`: a Deployment selector can't change, so altering it breaks upgrades */}}
{{- define "kube-siesta.selectorLabels" -}}
app: {{ include "kube-siesta.fullname" . }}
{{- end }}

{{/* The selected cloud section (.Values.aks or .Values.eks) */}}
{{- define "kube-siesta.cloud" -}}
{{- if not (has .Values.provider (list "aks" "eks")) }}
{{- fail (printf "provider must be aks or eks, got %q" .Values.provider) }}
{{- end }}
{{- toYaml (index .Values .Values.provider) }}
{{- end }}

{{/* ServiceAccount annotations: the cloud identity plus serviceAccount.annotations */}}
{{- define "kube-siesta.serviceAccountAnnotations" -}}
{{- $cloud := include "kube-siesta.cloud" . | fromYaml }}
{{- $annotations := dict }}
{{- if eq .Values.provider "aks" }}
{{- $_ := set $annotations "azure.workload.identity/client-id" (required "aks.workloadIdentityClientId is required" $cloud.workloadIdentityClientId) }}
{{- else }}
{{- $_ := set $annotations "eks.amazonaws.com/role-arn" (required "eks.roleArn is required" $cloud.roleArn) }}
{{- end }}
{{- toYaml (merge (deepCopy .Values.serviceAccount.annotations) $annotations) }}
{{- end }}

{{/* ConfigMap data: `config` plus the cloud and Flux/Argo CD/KEDA settings; lists and maps as JSON, the rest as strings */}}
{{- define "kube-siesta.configData" -}}
{{- $cloud := include "kube-siesta.cloud" . | fromYaml }}
{{- $data := deepCopy .Values.config }}
{{- $_ := set $data "SIESTA_PROVIDER" .Values.provider }}
{{- if eq .Values.provider "aks" }}
{{- $_ := set $data "SIESTA_AZURE_SUBSCRIPTION_ID" (required "aks.subscriptionId is required" $cloud.subscriptionId) }}
{{- $_ := set $data "SIESTA_AZURE_RESOURCE_GROUP" (required "aks.resourceGroup is required" $cloud.resourceGroup) }}
{{- $_ := set $data "SIESTA_AKS_CLUSTER_NAME" (required "aks.clusterName is required" $cloud.clusterName) }}
{{- else }}
{{- $_ := set $data "SIESTA_AWS_REGION" (required "eks.region is required" $cloud.region) }}
{{- $_ := set $data "SIESTA_EKS_CLUSTER_NAME" (required "eks.clusterName is required" $cloud.clusterName) }}
{{- $_ := set $data "SIESTA_SYSTEM_NODEPOOLS" ($cloud.systemNodepools | default list) }}
{{- $_ := set $data "SIESTA_AUTOSCALED_NODEPOOLS" ($cloud.autoscaledNodepools | default list) }}
{{- end }}
{{- $_ := set $data "SIESTA_FLUX_ENABLED" .Values.flux.enabled }}
{{- if .Values.flux.enabled }}
{{- $_ := set $data "SIESTA_FLUX_NAMESPACE" .Values.flux.namespace }}
{{- $_ := set $data "SIESTA_REQUIRE_FLUX_ISOLATION" .Values.flux.requireIsolation }}
{{- end }}
{{- $_ := set $data "SIESTA_ARGOCD_ENABLED" .Values.argocd.enabled }}
{{- if .Values.argocd.enabled }}
{{- $_ := set $data "SIESTA_ARGOCD_NAMESPACE" .Values.argocd.namespace }}
{{- $_ := set $data "SIESTA_REQUIRE_ARGOCD_ISOLATION" .Values.argocd.requireIsolation }}
{{- end }}
{{- $_ := set $data "SIESTA_KEDA_ENABLED" .Values.keda.enabled }}
{{- range $key, $value := $data }}
{{- if or (kindIs "slice" $value) (kindIs "map" $value) }}
{{ $key }}: {{ toJson $value | squote }}
{{- else }}
{{ $key }}: {{ toString $value | quote }}
{{- end }}
{{- end }}
{{- end }}
