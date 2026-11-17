# Installation

## Prerequisites

- An AKS or EKS cluster, with `kubectl` access and Helm 3.
- **AKS**: the cluster's OIDC issuer and Workload Identity enabled, and the Azure CLI (`az`) logged in with rights to create a managed identity and role assignment.
- **EKS**: an IAM OIDC provider for the cluster, and the AWS CLI with rights to create an IAM role.
- Access from the cluster to Docker Hub, or your own registry if you build the image yourself.
- The permissions listed in [Permissions](permissions.md), or an admin to run the identity script and the install.
- A node pool that stays up during off hours (a system pool or node group) for the kube-siesta pod.

## 1. Create the cloud identity

The app needs cloud credentials only to read and scale node pools. Run the matching script once per cluster. Both use the `kube-siesta` namespace and ServiceAccount by default; set `NAMESPACE` / `SERVICE_ACCOUNT` if you install under other names.

**AKS**

```sh
RG=<resource group> CLUSTER=<cluster> deploy/setup-azure-identity.sh
```

Creates a managed identity (`kube-siesta-<cluster>`, or `IDENTITY`), a federated credential for the ServiceAccount, and the *Azure Kubernetes Service Contributor* role on the cluster only. It prints the value for `aks.workloadIdentityClientId`.

**EKS**

```sh
AWS_REGION=<region> CLUSTER=<cluster> deploy/setup-aws-irsa.sh
```

Creates an IAM role (`kube-siesta-<cluster>`, or `ROLE`) that only the ServiceAccount can assume. It may describe Auto Scaling Groups, update ASGs tagged as owned by this cluster, and update this cluster's managed nodegroups. It prints the value for `eks.roleArn`.

## 2. Choose the image

The chart uses the published image [`docker.io/ashutoshojha5/kubesiesta`](https://hub.docker.com/r/ashutoshojha5/kubesiesta) by default, built for amd64 and arm64 nodes, so nothing needs building. Its tags:

| Tag | Points to |
|---|---|
| `latest` | The latest commit on the main branch (the chart's default) |
| `X.Y.Z`, `X.Y` | A release, e.g. `0.1.0`; use one for production so upgrades are deliberate |
| `sha-<commit>` | One specific commit |

To use your own build instead (e.g. a private registry), build and push it, and set `image.repository` and `image.tag`:

```sh
docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/kube-siesta:<tag> --push .
```

## 3. Write your values file

Copy `deploy/helm/kube-siesta/values.yaml` and change:

1. `image.repository` and `image.tag`.
2. `provider` (`aks` or `eks`) and that cloud's section: cluster details and the identity from step 1.
3. How your workloads are deployed: `flux.enabled: true`, `argocd.enabled: true`, or neither for plain manifests. See [Flux, Argo CD and KEDA](gitops-and-keda.md) for the labels each needs.
4. `keda.enabled: true` if the cluster runs KEDA.
5. Under `config`: which namespaces to manage (`SIESTA_MANAGED_NAMESPACES`), which to protect, the timezone and the off-hours windows.
6. `ingress` if you want the UI behind an ingress.

Every value is explained in [Helm values](helm-values.md).

## 4. Install

```sh
helm upgrade --install kube-siesta deploy/helm/kube-siesta \
  -n kube-siesta --create-namespace -f my-values.yaml
```

Check it started:

```sh
kubectl -n kube-siesta rollout status deploy/kube-siesta
kubectl -n kube-siesta logs deploy/kube-siesta
```

A wrong or missing setting stops the app at startup with a message naming the setting.

On a GitOps cluster you can deploy the chart the same way your apps are deployed: a Flux `HelmRelease` or an Argo CD `Application` pointing at the chart, with your values inline.

## 5. Open the UI

Without an ingress:

```sh
kubectl -n kube-siesta port-forward svc/kube-siesta 8000:80
```

Then open http://localhost:8000. Check the `/config` page: it shows the live settings and when the next off window starts.

> **The app has no login.** Anyone who can reach it can wake, sleep and scale. Keep the ingress reachable only from your VPN or VPC.

## Upgrading and uninstalling

- `helm upgrade` with changed values restarts the pod automatically (the ConfigMap's checksum is a pod annotation).
- `helm uninstall` keeps the state volume (`helm.sh/resource-policy: keep`), so a reinstall picks up saved node counts and leases. Delete the `kube-siesta-data` PVC yourself to remove it.
- Before uninstalling for good, wake every namespace, or their replica counts stay saved only in annotations.
