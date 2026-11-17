# KubeSiesta

KubeSiesta scales a Kubernetes cluster down outside working hours and lets developers wake what they need from a small web UI.

- **Off hours** (a weekly window, optionally every night too): the workloads in the namespaces you choose are scaled to 0, CronJobs are suspended, and node pools shrink. When off hours end, everything comes back as it was.
- **On demand**: during off hours anyone with access can wake a namespace from the UI. Nodes come up automatically, and the namespace goes back to sleep when its lease runs out.
- Works on **AKS** and **EKS**, and keeps **Flux**, **Argo CD** and **KEDA** from undoing the scale-down. Also works with plain manifests.

## Deploy with Helm

1. Create the cloud identity the app uses to scale node pools (once per cluster):

   ```sh
   RG=<resource group> CLUSTER=<cluster> deploy/setup-azure-identity.sh   # AKS
   AWS_REGION=<region> CLUSTER=<cluster> deploy/setup-aws-irsa.sh          # EKS
   ```

2. Use the published image [`ashutoshojha5/kubesiesta`](https://hub.docker.com/r/ashutoshojha5/kubesiesta) on Docker Hub (the chart's default, amd64 and arm64), or build your own from the `Dockerfile`.

3. Copy `deploy/helm/kube-siesta/values.yaml` and set:
   - `image.tag` (and `image.repository` if you built your own)
   - `provider: aks` or `eks`, and that section (cluster details and the identity printed in step 1)
   - `flux.enabled` or `argocd.enabled` if a GitOps tool deploys your workloads, `keda.enabled` if you use KEDA
   - under `config`: the namespaces to manage, the timezone and the off-hours windows

4. Install:

   ```sh
   helm upgrade --install kube-siesta deploy/helm/kube-siesta \
     -n kube-siesta --create-namespace -f my-values.yaml
   ```

5. Open the UI (or set up `ingress`):

   ```sh
   kubectl -n kube-siesta port-forward svc/kube-siesta 8000:80
   ```

> The app has no login. Keep it reachable only from your VPN or VPC.

With Flux or Argo CD, label each namespace's Kustomization or Application with `<prefix>/namespace: <namespace>`; see [Flux, Argo CD and KEDA](docs/gitops-and-keda.md).

## Documentation

Everything else, including every setting, is in [docs](docs/README.md):

[How it works](docs/how-it-works.md) · [Installation](docs/installation.md) · [Helm values](docs/helm-values.md) · [Configuration](docs/configuration.md) · [AKS and EKS](docs/clouds.md) · [Flux, Argo CD and KEDA](docs/gitops-and-keda.md) · [UI and API](docs/ui-and-api.md) · [Permissions](docs/permissions.md) · [Operations](docs/operations.md) · [Development](docs/development.md)

## License

[MIT](LICENSE): free to use, change and share, including commercially.
