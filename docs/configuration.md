# Configuration

The app reads every setting from a `SIESTA_*` environment variable. In the cluster they come from the chart's ConfigMap (see [Helm values](helm-values.md)); locally from a `.env` file (see [Development](development.md)).

- Settings marked **required** have no default; if one is missing the app stops at startup and names it.
- A value that is invalid (a bad timezone, an unknown weekday, a malformed list) also stops it at startup.
- Lists and maps are JSON: `["dev*", "qa"]`, `{"systempool": 1}`. Times are `HH:MM` (24-hour).
- The `/config` page shows the live values, with the same explanations behind the **i** buttons. `/api/config` returns them as JSON.
- Settings are read at startup. The Helm chart restarts the pod when they change; locally, restart the app.

## Namespaces

| Setting | Default | Description |
|---|---|---|
| `SIESTA_MANAGED_NAMESPACES` | required | Namespaces put to sleep when off hours start and woken when they end, as [fnmatch](https://docs.python.org/3/library/fnmatch.html) patterns: `dev*` matches `dev1` and `dev-api`; `?` matches one character. The UI lists these by default. |
| `SIESTA_PROTECTED_NAMESPACES` | required | Namespaces the app never changes and never lists, as fnmatch patterns. Include the namespace KubeSiesta runs in, `kube-*`, and shared infrastructure (ingress controller, cert-manager, monitoring). Protected wins when a namespace matches both lists. |

The Flux namespace (when Flux is enabled) and the Argo CD namespace (when Argo CD is enabled) are always protected, whether listed or not.

## Cloud

| Setting | Default | Description |
|---|---|---|
| `SIESTA_PROVIDER` | `aks` | `aks` or `eks`. Only the chosen cloud's settings below are required. |

**AKS** (all required when the provider is `aks`):

| Setting | Description |
|---|---|
| `SIESTA_AZURE_SUBSCRIPTION_ID` | Subscription that contains the cluster's resource group. |
| `SIESTA_AZURE_RESOURCE_GROUP` | Resource group of the cluster. |
| `SIESTA_AKS_CLUSTER_NAME` | Name of the cluster whose node pools are managed. |

**EKS**:

| Setting | Default | Description |
|---|---|---|
| `SIESTA_AWS_REGION` | required | Region of the cluster and its Auto Scaling Groups. |
| `SIESTA_EKS_CLUSTER_NAME` | required | Cluster name. Node pools are the Auto Scaling Groups tagged `kubernetes.io/cluster/<name>`. |
| `SIESTA_SYSTEM_NODEPOOLS` | `[]` | Node groups treated as system pools: never below 1 node, shrunk last and grown first. EKS has no system/user mode, so list the group the kube-siesta pod and add-ons run on. |
| `SIESTA_AUTOSCALED_NODEPOOLS` | `[]` | Managed node groups owned by the cluster autoscaler; never scaled. Self-managed groups whose min and max differ are detected without being listed. |

See [AKS and EKS](clouds.md) for how pools are found and scaled.

## Node pools

| Setting | Default | Description |
|---|---|---|
| `SIESTA_PROTECTED_NODEPOOLS` | required | Pools never scaled, by the schedule or from the UI. Use `[]` for none. |
| `SIESTA_WEEKEND_NODE_COUNTS` | required | Node count per pool during off hours (weekly and nightly), e.g. `{"systempool": 1, "batch": 2}`. User pools not listed go to 0; system pools not listed are left alone. Use `{}` to take every user pool to 0. |
| `SIESTA_DEFAULT_WAKE_NODE_COUNT` | required | Node count used when a pool at 0 has to come up and no working-hours size was saved for it, e.g. a pool someone scaled to 0 by hand. |

Pools with the cluster autoscaler enabled are never scaled, whatever these settings say.

## Schedule

All times are in `SIESTA_TIMEZONE`. See [How it works](how-it-works.md#off-hours).

| Setting | Default | Description |
|---|---|---|
| `SIESTA_TIMEZONE` | required | [IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) such as `UTC`, `Europe/Berlin`, `Asia/Kolkata`. Also used for the times shown in the UI. |
| `SIESTA_WEEKEND_START_DAY` | required | Day the weekly window starts: `mon` … `sun`. |
| `SIESTA_WEEKEND_START_TIME` | required | Time it starts on that day, e.g. `21:00`. |
| `SIESTA_WEEKEND_END_DAY` | required | Day the weekly window ends. |
| `SIESTA_WEEKEND_END_TIME` | required | Time it ends on that day, e.g. `07:00`. |
| `SIESTA_NIGHTLY_ENABLED` | required | `true` makes every night an off window too. |
| `SIESTA_NIGHTLY_START_TIME` | required | Time the nightly window starts. Required even when nightly is off. |
| `SIESTA_NIGHTLY_END_TIME` | required | Time it ends; if earlier than the start, it ends the next day. |
| `SIESTA_LEASE_HOURS` | required | How long a namespace woken during off hours stays up before going back to sleep. **Extend** in the UI resets it. |
| `SIESTA_SCHEDULE_INTERVAL_SECONDS` | required | How often the scheduler checks the windows and leases. `300` (5 minutes) is a good value; a window starts or ends at most this long late. |

## Flux, Argo CD and KEDA

See [Flux, Argo CD and KEDA](gitops-and-keda.md) for what each does and the labels it needs.

| Setting | Default | Description |
|---|---|---|
| `SIESTA_FLUX_ENABLED` | `true` | Whether the cluster uses Flux. Set `false` for Argo CD or plain manifests. |
| `SIESTA_FLUX_NAMESPACE` | `flux-system` | Namespace of the Flux Kustomizations. Always protected when Flux is enabled. |
| `SIESTA_REQUIRE_FLUX_ISOLATION` | `true` | Refuse to sleep a namespace unless a Flux Kustomization is labelled for it. |
| `SIESTA_ARGOCD_ENABLED` | `false` | Whether the cluster uses Argo CD. |
| `SIESTA_ARGOCD_NAMESPACE` | `argocd` | Namespace Argo CD runs in. Always protected when Argo CD is enabled. |
| `SIESTA_REQUIRE_ARGOCD_ISOLATION` | `true` | Refuse to sleep a namespace unless an Argo CD Application is labelled for it. |
| `SIESTA_KEDA_ENABLED` | `false` | Pause KEDA ScaledObjects and ScaledJobs while their namespace sleeps. |

Note that Flux is on by default in the app (not in the Helm chart, which sets these explicitly). When running locally without Flux, set `SIESTA_FLUX_ENABLED=false`.

## Labels and annotations

| Setting | Default | Description |
|---|---|---|
| `SIESTA_LABEL_PREFIX` | `kube-siesta.io` | Domain prefix of every label and annotation the app reads and writes. Must be a valid DNS name (lowercase letters, digits, `-` and `.`). Use a domain you own, e.g. `kube-siesta.example.com`. |

With prefix `<prefix>`, the app uses:

| Name | On | Meaning |
|---|---|---|
| `<prefix>/original-replicas` | Deployments, StatefulSets | Replica count to restore on wake. Present means asleep. |
| `<prefix>/suspended` | CronJobs, Flux objects, KEDA objects | Paused by the app; resumed on wake. |
| `<prefix>/auto-sync` | Argo CD Applications | The automated sync policy the app removed; put back on wake. |
| `<prefix>/namespace` | Flux Kustomizations, Argo CD Applications | Label **you** set to say which namespace the object deploys. |

**Wake every namespace before changing the prefix.** The app only recognises markers with the current prefix, so anything asleep under the old one would not be woken.

## Other

| Setting | Default | Description |
|---|---|---|
| `SIESTA_STATE_DB_PATH` | required | Path of the SQLite state file. In the cluster, a file on the state volume (`/data/kube-siesta.db`); locally e.g. `./data/kube-siesta.db`. The directory is created if missing. |
| `SIESTA_CONFIG_URL` | none | Link shown on the `/config` page to where the settings are edited, e.g. the values file in your Git repository. |
