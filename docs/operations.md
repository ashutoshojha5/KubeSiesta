# Operations

## Day to day

- **Logs**: `kubectl -n kube-siesta logs deploy/kube-siesta`. Every schedule run, sleep, wake and failure is logged.
- **Who did what**: the audit log in the UI's Activity section.
- **Changing settings**: edit your values file and `helm upgrade`. The pod restarts and the new settings show on `/config`. If the pod doesn't come back, the log names the bad setting.
- **Adding a namespace to the schedule**: add it (or a matching pattern) to `SIESTA_MANAGED_NAMESPACES`. With Flux or Argo CD, also label its Kustomization or Application.

## Things to keep in mind

- **One replica only.** The state file has one writer and the scheduler runs in the pod. The chart enforces this; don't scale the Deployment up.
- **Run the pod on a pool that stays up**, or it scales away its own node. If it does get evicted, it restarts on a remaining node and catches up.
- **A failed sleep keeps the nodes.** If any managed namespace can't be put to sleep (for example it isn't isolated), the node pools are left up and the run is retried every interval until it succeeds. Fix the namespace or remove it from the managed list.
- **Hand-scaled pools.** During off hours, scaling a pool from the UI doesn't change the size restored when off hours end.
- **Deploying during off hours** to a sleeping namespace brings it back up; the next schedule run puts it back to sleep unless it has a lease. Wake it in the UI to keep it up.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Sleep fails with "no Flux Kustomization labelled ..." or "no Argo CD Application labelled ..." | The namespace has no Kustomization/Application with the `<prefix>/namespace` label. Add the label, or turn off `requireIsolation` (GitOps will then undo the sleep). |
| A namespace wakes up again by itself | Something else scales it: an unlabelled GitOps object, a CI deploy, an HPA or KEDA (enable `keda`), or an operator that manages the workloads. |
| Argo CD column shows **re-enabled** | An ApplicationSet or parent app turns auto-sync back on. See [ApplicationSets and app of apps](gitops-and-keda.md#applicationsets-and-app-of-apps). |
| Node pools didn't go down | A namespace failed to sleep (see the log), or a namespace still has an active lease. |
| Node pools didn't come back | The window wasn't scaled down by the app (e.g. installed mid-weekend), or scaling failed; check the log and the cloud identity's permissions. |
| `403` or authorization errors from Azure/AWS in the log | The identity is missing or the ServiceAccount annotation is wrong. Re-check `aks.workloadIdentityClientId` / `eks.roleArn`. |
| Pod won't start, log names a `SIESTA_*` setting | That setting is missing or invalid. |

## Fixing stuck state by hand

All sleep state lives on the objects, so it can be inspected and fixed with `kubectl`:

- **Workloads**: a Deployment or StatefulSet with `<prefix>/original-replicas` is asleep. Wake the namespace from the UI, or scale it to that value and remove the annotation.
- **CronJobs**: `<prefix>/suspended` means the app suspended it. Set `spec.suspend: false` and remove the annotation.
- **Flux**: the same annotation on a Kustomization or HelmRelease; `flux resume` it and remove the annotation.
- **Argo CD**: an Application with `<prefix>/auto-sync` has auto-sync off; the annotation holds the `spec.syncPolicy.automated` to put back.
- **KEDA**: `<prefix>/suspended` plus `autoscaling.keda.sh/paused`; remove both.

```sh
# Find everything asleep (default prefix shown)
kubectl get deploy,sts -A -o json | jq -r '.items[]
  | select(.metadata.annotations["kube-siesta.io/original-replicas"])
  | "\(.metadata.namespace)/\(.metadata.name)"'
```

## Permissions

The app's ServiceAccount can only get, list and patch namespaces, workloads, CronJobs and the Flux/Argo CD/KEDA objects you enable; it can't create or delete anything, or read Secrets. Its cloud identity can only read and scale this cluster's node pools. Full lists, and what you need to deploy, are in [Permissions](permissions.md).

## Security

- The UI has **no authentication**. Anyone who reaches it can wake, sleep and scale. Expose it only on an internal ingress or through your VPN, and use the ingress's IP allow-list.
- Names in the audit log are what users type; the IP address next to them is from the request (or `X-Forwarded-For` behind an ingress).
- The container runs as a non-root user with a read-only root filesystem and no Linux capabilities.
