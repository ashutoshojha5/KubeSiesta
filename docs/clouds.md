# AKS and EKS

The cloud only matters for node pools: finding them, deciding which are system pools, and scaling them. Workloads are handled the same way on both through the Kubernetes API.

## AKS

**Node pools** are the cluster's agent pools, both VM Scale Set and VirtualMachines pools.

- **System or user**: from the pool's mode in AKS. System pools never go below 1 node.
- **Autoscaled pools**: pools with the cluster autoscaler enabled are never scaled.
- **Scaling** sets the pool's node count through the Azure API. It takes a few minutes; the UI shows it in the Activity list.

**Authentication**: Azure Workload Identity. The pod's ServiceAccount is federated with a managed identity that has the *Azure Kubernetes Service Contributor* role on this cluster only. `deploy/setup-azure-identity.sh` sets this up. Locally, the app uses your `az login` credentials.

**Running the pod**: on a system pool (`kubernetes.azure.com/mode: system`). AKS system pools are often tainted `CriticalAddonsOnly`, so the chart adds that toleration.

## EKS

**Node pools** are the EC2 Auto Scaling Groups tagged `kubernetes.io/cluster/<SIESTA_EKS_CLUSTER_NAME>`. Each is named after its EKS or eksctl nodegroup tag, falling back to the ASG name.

- **System or user**: EKS has no such mode, so the groups listed in `SIESTA_SYSTEM_NODEPOOLS` are system pools (never below 1 node); all others are user pools.
- **Self-managed node groups** are scaled on the ASG itself, setting min, max and desired to the same count. A group whose min and max differ is treated as owned by the cluster autoscaler and never scaled.
- **Managed node groups** are scaled through the EKS API, because EKS would undo changes made directly on their ASG. EKS needs max ≥ 1, so a group at 0 nodes is set to min 0 / max 1 / desired 0. Because of that, the app can't tell an autoscaled managed group from a manual one: list autoscaled managed groups in `SIESTA_AUTOSCALED_NODEPOOLS`.

**Authentication**: IRSA (IAM Roles for Service Accounts). The ServiceAccount carries the ARN of a role that may describe ASGs, update ASGs owned by this cluster, and update this cluster's managed nodegroups. `deploy/setup-aws-irsa.sh` sets this up. Locally, the app uses your AWS credentials (`AWS_PROFILE` etc.).

**Running the pod**: on a node group listed in `SIESTA_SYSTEM_NODEPOOLS`. The chart selects it with `eks.amazonaws.com/nodegroup`, a label only managed node groups have; for self-managed groups, label the nodes yourself and set `eks.nodeSelector`.

## Choosing off-hours sizes

`SIESTA_WEEKEND_NODE_COUNTS` uses the pool names shown in the UI's **Node pools** table. A common setup:

- system pool: 1 node (enough for add-ons, KubeSiesta, and the controllers that wake things up),
- user pools: 0 (the default for unlisted user pools),
- a pool that must keep running something small over the weekend: list it with a count.
