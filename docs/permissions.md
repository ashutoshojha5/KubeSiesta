# Permissions

Three sets of permissions are involved:

1. **You, to deploy**: run the one-time identity script, push the image, and install the Helm chart.
2. **The app, at runtime**: what its cloud identity and its Kubernetes ServiceAccount may do. The setup script and the chart create exactly these.
3. **The cluster, to pull the image.**

Names below use the defaults: namespace and ServiceAccount `kube-siesta`, identity/role `kube-siesta-<cluster>`.

## Azure (AKS)

### To deploy

**Run `deploy/setup-azure-identity.sh`**. It reads the cluster, creates a managed identity with a federated credential, and assigns a role on the cluster.

| Role | Scope | Needed for |
|---|---|---|
| Managed Identity Contributor | Resource group of the cluster | Creating the managed identity and its federated credential |
| Role Based Access Control Administrator | The AKS cluster | Assigning the role to the identity, and reading the cluster (`az aks show`) |

To tighten the second role, add a condition that allows assigning only the *Azure Kubernetes Service Contributor Role*. **Owner** on the resource group also works but grants much more. The cluster must already have the OIDC issuer and Workload Identity enabled; turning them on needs write access to the cluster and is normally done by whoever owns it.

**Install the chart**. You need cluster credentials and enough Kubernetes rights:

| What | Role / permission |
|---|---|
| Get kubeconfig | *Azure Kubernetes Service Cluster User Role* on the cluster |
| Kubernetes rights, cluster uses **Kubernetes RBAC** | The [Kubernetes deployer role](#kubernetes-deployer-role) below, bound to your user or group |
| Kubernetes rights, cluster uses **Azure RBAC for Kubernetes** | *Azure Kubernetes Service RBAC Cluster Admin* on the cluster. The namespace-scoped *RBAC Admin* is not enough, because the chart creates a ClusterRole and ClusterRoleBinding. |

### The app at runtime

| Role | Scope | Used for |
|---|---|---|
| Azure Kubernetes Service Contributor Role | The AKS cluster only | Listing agent pools and changing their node count |

Assigned to the managed identity by the setup script. The pod gets it through Workload Identity; no secret is stored.

### Image

| Who | Role | Scope |
|---|---|---|
| You or your CI, to push | AcrPush | The container registry |
| The cluster, to pull | AcrPull for the cluster's kubelet identity (`az aks update --attach-acr <registry>`, which needs Owner or User Access Administrator on the registry) | The container registry |

## AWS (EKS)

### To deploy

**Run `deploy/setup-aws-irsa.sh`**. It reads the cluster's OIDC issuer, checks the OIDC provider exists, creates an IAM role and puts an inline policy on it. Minimum IAM policy for the person running it:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "eks:DescribeCluster",
     "Resource": "arn:aws:eks:<region>:<account>:cluster/<cluster>"},
    {"Effect": "Allow", "Action": "iam:GetOpenIDConnectProvider",
     "Resource": "arn:aws:iam::<account>:oidc-provider/oidc.eks.<region>.amazonaws.com/id/*"},
    {"Effect": "Allow", "Action": ["iam:CreateRole", "iam:PutRolePolicy"],
     "Resource": "arn:aws:iam::<account>:role/kube-siesta-*"}
  ]
}
```

(`sts:GetCallerIdentity` needs no permission.) Whoever can put a policy on a role can give it any rights, so many teams have an IAM administrator run this script once instead of granting these actions. The cluster must already have an IAM OIDC provider (`eksctl utils associate-iam-oidc-provider`), which needs `iam:CreateOpenIDConnectProvider`.

**Install the chart**:

| What | Permission |
|---|---|
| Get kubeconfig (`aws eks update-kubeconfig`) | `eks:DescribeCluster` on the cluster |
| Kubernetes rights | An EKS access entry for your IAM principal with *AmazonEKSClusterAdminPolicy*, or an access entry (or `aws-auth` mapping) to a Kubernetes group bound to the [Kubernetes deployer role](#kubernetes-deployer-role). *AmazonEKSAdminPolicy* is not enough: it can't create the chart's ClusterRole and ClusterRoleBinding. |

### The app at runtime

The IAM role created by the setup script, assumed through IRSA:

| Action | Resource | Used for |
|---|---|---|
| `autoscaling:DescribeAutoScalingGroups` | `*` (AWS doesn't support narrowing it) | Finding the cluster's node groups |
| `autoscaling:UpdateAutoScalingGroup` | Only ASGs tagged `kubernetes.io/cluster/<cluster>=owned` | Scaling self-managed node groups |
| `eks:UpdateNodegroupConfig` | Only this cluster's managed nodegroups | Scaling managed node groups |

Only the `kube-siesta` ServiceAccount in the `kube-siesta` namespace can assume the role.

### Image

| Who | Permission |
|---|---|
| You or your CI, to push | `ecr:GetAuthorizationToken`, plus `ecr:BatchCheckLayerAvailability`, `ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`, `ecr:PutImage` on the repository |
| The nodes, to pull | *AmazonEC2ContainerRegistryReadOnly* on the node role (managed node groups have it by default) |

## Kubernetes

### Kubernetes deployer role

The least you need to install and upgrade the chart, instead of `cluster-admin`. Have a cluster admin create the namespace and apply this once:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: kube-siesta
---
# Everything inside the namespace (Deployment, Service, PVC, ConfigMap, ServiceAccount, Ingress,
# and the Secrets in which Helm keeps release history)
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: kube-siesta-deployer
  namespace: kube-siesta
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: admin            # built-in, limited to this namespace by the RoleBinding
subjects:
  - kind: Group          # or User; the name your cloud login maps to
    name: kube-siesta-deployers
    apiGroup: rbac.authorization.k8s.io
---
# The chart's cluster-scoped ClusterRole and ClusterRoleBinding. `escalate` and `bind` let you create
# them without holding every permission they grant.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kube-siesta-deployer
rules:
  - apiGroups: ["rbac.authorization.k8s.io"]
    resources: ["clusterroles", "clusterrolebindings"]
    verbs: ["get", "list", "create", "update", "patch", "delete", "bind", "escalate"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: kube-siesta-deployer
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: kube-siesta-deployer
subjects:
  - kind: Group
    name: kube-siesta-deployers
    apiGroup: rbac.authorization.k8s.io
```

Then install without `--create-namespace`:

```sh
helm upgrade --install kube-siesta deploy/helm/kube-siesta -n kube-siesta -f my-values.yaml
```

`escalate` and `bind` on ClusterRoles are powerful (they allow granting any permission), so keep this group small, or let a cluster admin do the install.

### The app at runtime

The chart's ClusterRole:

| Resources | Verbs | When |
|---|---|---|
| namespaces | get, list | always |
| deployments, statefulsets | get, list, patch | always |
| cronjobs | get, list, patch | always |
| Flux kustomizations, helmreleases | get, list, patch | `flux.enabled` |
| Argo CD applications | get, list, patch | `argocd.enabled` |
| KEDA scaledobjects, scaledjobs | get, list, patch | `keda.enabled` |

It can't create or delete anything, and can't read Secrets or Pods.

## Summary

| | Azure | AWS |
|---|---|---|
| Identity setup (once) | Managed Identity Contributor on the RG + RBAC Administrator on the cluster | `eks:DescribeCluster`, `iam:GetOpenIDConnectProvider`, `iam:CreateRole`/`PutRolePolicy` on `kube-siesta-*` |
| Kubernetes install | AKS Cluster User Role + deployer role (or AKS RBAC Cluster Admin) | `eks:DescribeCluster` + access entry with ClusterAdmin or the deployer role |
| Push image | AcrPush | ECR push actions |
| App runtime (cloud) | AKS Contributor Role on the cluster | Describe/Update ASG (cluster-owned), UpdateNodegroupConfig (this cluster) |
| App runtime (Kubernetes) | Chart ClusterRole: get/list/patch on workloads, CronJobs and enabled integrations | Same |
