# KubeSiesta documentation

KubeSiesta scales a Kubernetes cluster down outside working hours and lets developers wake what they need from a small web UI. These pages explain how it works and every setting it has.

| Page | Read it to |
|---|---|
| [How it works](how-it-works.md) | Understand off hours, sleeping and waking, leases and the schedule |
| [Installation](installation.md) | Set up the cloud identity, build the image and install the Helm chart |
| [Helm values](helm-values.md) | Look up any value in the chart's `values.yaml` |
| [Configuration](configuration.md) | Look up any `SIESTA_*` setting the app reads |
| [AKS and EKS](clouds.md) | See how node pools are found, scaled and authenticated on each cloud |
| [Flux, Argo CD and KEDA](gitops-and-keda.md) | Stop GitOps tools and autoscalers from undoing a scale-down |
| [UI and API](ui-and-api.md) | Use the web UI and the HTTP endpoints |
| [Permissions](permissions.md) | See the minimum Azure, AWS and Kubernetes permissions to deploy and run it |
| [Operations](operations.md) | Run it day to day, fix stuck state, know what it is allowed to do |
| [Development](development.md) | Run it locally, run the tests, find your way around the code |

## Words used in these docs

- **Off hours**: the times when the cluster should be scaled down, the weekly window plus (optionally) every night.
- **Managed namespace**: a namespace the schedule puts to sleep, matched by `SIESTA_MANAGED_NAMESPACES`.
- **Protected namespace / node pool**: never touched, by the schedule or from the UI.
- **Sleep**: scale every Deployment and StatefulSet in a namespace to 0, remembering the replica counts.
- **Wake**: put those replica counts back.
- **Lease**: how long a namespace woken during off hours stays up before it goes back to sleep.
- **System / user node pool**: system pools run cluster add-ons and keep at least one node; user pools can go to 0.
- **`<prefix>`**: the label/annotation domain set by `SIESTA_LABEL_PREFIX` (default `kube-siesta.io`).
