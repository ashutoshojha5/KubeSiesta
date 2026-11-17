# Helm values

All values of the chart in `deploy/helm/kube-siesta`. The chart's own `values.yaml` holds the defaults and is meant to be copied as your values file.

The chart creates: a Deployment (always 1 replica, `Recreate` strategy), Service, PersistentVolumeClaim, ConfigMap, ServiceAccount, ClusterRole and ClusterRoleBinding, and optionally an Ingress. All named after the release (`kube-siesta` when the release is called that).

## Image

| Value | Default | Description |
|---|---|---|
| `image.repository` | `docker.io/ashutoshojha5/kubesiesta` | Image to run, without the tag. The [published image](https://hub.docker.com/r/ashutoshojha5/kubesiesta) supports amd64 and arm64 nodes; set your own registry if you build the image yourself. |
| `image.tag` | `latest` | Image tag: `latest` follows the main branch, `X.Y.Z` / `X.Y` are releases, `sha-<commit>` is one commit. |
| `image.pullPolicy` | `Always` | Pull policy. `Always` suits a moving tag like `latest`; use `IfNotPresent` with fixed versions. |

## Cloud

| Value | Default | Description |
|---|---|---|
| `provider` | `aks` | `aks` or `eks`. Selects which of the two sections below is used; the other is ignored. Anything else fails the install. |

The selected section sets the app's cloud settings, the workload identity, where the pod runs and the volume's storage class.

### `aks`

| Value | Default | Description |
|---|---|---|
| `aks.subscriptionId` | example ID | Azure subscription of the cluster. Becomes `SIESTA_AZURE_SUBSCRIPTION_ID`. Required. |
| `aks.resourceGroup` | `my-resource-group` | Resource group of the cluster. Becomes `SIESTA_AZURE_RESOURCE_GROUP`. Required. |
| `aks.clusterName` | `my-cluster` | AKS cluster name. Becomes `SIESTA_AKS_CLUSTER_NAME`. Required. |
| `aks.workloadIdentityClientId` | example ID | Client ID printed by `deploy/setup-azure-identity.sh`. Set as the ServiceAccount's `azure.workload.identity/client-id` annotation; the pod also gets the `azure.workload.identity/use: "true"` label. Required. |
| `aks.nodeSelector` | `kubernetes.azure.com/mode: system` | Keeps the pod on a system pool, which never goes to 0. |
| `aks.tolerations` | `CriticalAddonsOnly` | Lets the pod run on system pools that are tainted for add-ons only. |
| `aks.storageClassName` | `managed-csi` | Storage class of the state volume. |

### `eks`

| Value | Default | Description |
|---|---|---|
| `eks.region` | `us-east-1` | AWS region of the cluster. Becomes `SIESTA_AWS_REGION`. Required. |
| `eks.clusterName` | `my-cluster` | EKS cluster name. Becomes `SIESTA_EKS_CLUSTER_NAME`. Required. |
| `eks.systemNodepools` | `["system"]` | Node groups treated as system pools (kept at 1 or more nodes). Becomes `SIESTA_SYSTEM_NODEPOOLS`. |
| `eks.autoscaledNodepools` | `[]` | Managed node groups owned by the cluster autoscaler; never scaled. Becomes `SIESTA_AUTOSCALED_NODEPOOLS`. |
| `eks.roleArn` | example ARN | Role ARN printed by `deploy/setup-aws-irsa.sh`. Set as the ServiceAccount's `eks.amazonaws.com/role-arn` annotation. Required. |
| `eks.nodeSelector` | `eks.amazonaws.com/nodegroup: system` | Keeps the pod on a node group listed in `systemNodepools`. This label exists on managed node groups only; for self-managed groups use a label of your own. |
| `eks.tolerations` | `[]` | Tolerations for that node group, if it is tainted. |
| `eks.storageClassName` | `gp3` | Storage class of the state volume. |

See [AKS and EKS](clouds.md) for what counts as a node pool on each cloud.

## Deployed by: Flux, Argo CD or plain manifests

Leave both `flux.enabled` and `argocd.enabled` false when workloads are applied as plain manifests. Turning one on also grants the RBAC it needs. Details in [Flux, Argo CD and KEDA](gitops-and-keda.md).

| Value | Default | Description |
|---|---|---|
| `flux.enabled` | `false` | Suspend the namespace's labelled Flux Kustomizations and its HelmReleases while it sleeps. Becomes `SIESTA_FLUX_ENABLED`. |
| `flux.namespace` | `flux-system` | Where the Kustomizations live; always protected. Becomes `SIESTA_FLUX_NAMESPACE`. |
| `flux.requireIsolation` | `true` | Refuse to sleep a namespace with no labelled Kustomization. Becomes `SIESTA_REQUIRE_FLUX_ISOLATION`. |
| `argocd.enabled` | `false` | Turn off automated sync on the namespace's labelled Applications while it sleeps. Becomes `SIESTA_ARGOCD_ENABLED`. |
| `argocd.namespace` | `argocd` | Where Argo CD runs; always protected. Becomes `SIESTA_ARGOCD_NAMESPACE`. |
| `argocd.requireIsolation` | `true` | Refuse to sleep a namespace with no labelled Application. Becomes `SIESTA_REQUIRE_ARGOCD_ISOLATION`. |

## KEDA

| Value | Default | Description |
|---|---|---|
| `keda.enabled` | `false` | Pause the namespace's ScaledObjects and ScaledJobs while it sleeps. Becomes `SIESTA_KEDA_ENABLED` and grants the RBAC. |

## App settings (`config`)

Every key under `config` is written to the ConfigMap as is and read by the app as an environment variable. Lists and maps are written as JSON; everything else as a string. See [Configuration](configuration.md) for each setting.

The defaults set in `values.yaml`:

| Key | Default |
|---|---|
| `SIESTA_MANAGED_NAMESPACES` | `["dev*"]` |
| `SIESTA_PROTECTED_NAMESPACES` | `["kube-*", "default", "kube-siesta"]` |
| `SIESTA_PROTECTED_NODEPOOLS` | `[]` |
| `SIESTA_WEEKEND_NODE_COUNTS` | `{}` |
| `SIESTA_DEFAULT_WAKE_NODE_COUNT` | `1` |
| `SIESTA_TIMEZONE` | `UTC` |
| `SIESTA_WEEKEND_START_DAY` / `_TIME` | `fri` / `"21:00"` |
| `SIESTA_WEEKEND_END_DAY` / `_TIME` | `mon` / `"07:00"` |
| `SIESTA_NIGHTLY_ENABLED` | `false` |
| `SIESTA_NIGHTLY_START_TIME` / `_END_TIME` | `"21:00"` / `"07:00"` |
| `SIESTA_LEASE_HOURS` | `8` |
| `SIESTA_STATE_DB_PATH` | `/data/kube-siesta.db` (the state volume is mounted at `/data`) |
| `SIESTA_SCHEDULE_INTERVAL_SECONDS` | `300` |
| `SIESTA_LABEL_PREFIX` | `kube-siesta.example.com` |

Notes:

- Don't set the cloud, Flux, Argo CD or KEDA settings here; the sections above set them and win.
- Quote times (`"07:00"`), or YAML may read them as numbers.
- Helm replaces lists instead of merging them: overriding `SIESTA_PROTECTED_NAMESPACES` means repeating the whole list.
- Optional settings not in the defaults, such as `SIESTA_CONFIG_URL`, can be added here.

## Kubernetes

| Value | Default | Description |
|---|---|---|
| `persistence.storageClassName` | `""` | Storage class of the state volume. Empty uses the cloud section's `storageClassName`. |
| `persistence.size` | `1Gi` | Size of the state volume. The database stays small; 1Gi is plenty. |
| `serviceAccount.annotations` | `{}` | Extra ServiceAccount annotations, added to the cloud identity annotation. |
| `podLabels` | `{}` | Extra pod labels. |
| `nodeSelector` | `{}` | When set, replaces the cloud section's `nodeSelector`. |
| `tolerations` | `[]` | When set, replaces the cloud section's `tolerations`. |
| `resources` | 50m CPU / 128Mi requested, 256Mi limit | Container resources. |
| `service.port` | `80` | Service port; the container listens on 8000. |

## Ingress

| Value | Default | Description |
|---|---|---|
| `ingress.enabled` | `false` | Create an Ingress for the UI. |
| `ingress.className` | `nginx` | Ingress class. |
| `ingress.host` | `kube-siesta.example.com` | Host name. Required when the ingress is enabled. |
| `ingress.tlsSecretName` | `""` | TLS secret for the host; empty serves plain HTTP. |
| `ingress.annotations` | `{}` | Ingress annotations, e.g. `nginx.ingress.kubernetes.io/whitelist-source-range: "10.0.0.0/8"` to allow only internal addresses. |

The app has no login, so restrict who can reach the ingress.
