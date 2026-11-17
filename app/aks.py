"""AKS node pool listing and scaling. Handles both VMSS pools and VirtualMachines pools."""

from azure.identity import DefaultAzureCredential
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.containerservice.models import AgentPool

from app.config import Settings
from app.nodepools import NodePool


def _is_vms_pool(pool: AgentPool) -> bool:
    return bool(pool.virtual_machines_profile and pool.virtual_machines_profile.scale
                and pool.virtual_machines_profile.scale.manual)


def _count(pool: AgentPool) -> int:
    if _is_vms_pool(pool):
        return sum(m.count or 0 for m in pool.virtual_machines_profile.scale.manual)
    return pool.count or 0


def _vm_size(pool: AgentPool) -> str:
    if _is_vms_pool(pool):
        return ", ".join(m.size for m in pool.virtual_machines_profile.scale.manual)
    return pool.vm_size or ""


class AksNodePools:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = ContainerServiceClient(DefaultAzureCredential(), settings.azure_subscription_id)

    def _args(self) -> tuple[str, str]:
        return self.settings.azure_resource_group, self.settings.aks_cluster_name

    def _to_model(self, pool: AgentPool) -> NodePool:
        return NodePool(
            name=pool.name,
            mode=getattr(pool.mode, "value", pool.mode) or "User",  # SDK returns an enum
            vm_size=_vm_size(pool),
            count=_count(pool),
            autoscaling=bool(pool.enable_auto_scaling),
            provisioning_state=pool.provisioning_state or "",
            protected=pool.name in self.settings.protected_nodepools,
        )

    def list(self) -> list[NodePool]:
        return sorted((self._to_model(p) for p in self.client.agent_pools.list(*self._args())),
                      key=lambda p: (p.mode != "System", p.name))

    def get(self, name: str) -> NodePool:
        return self._to_model(self.client.agent_pools.get(*self._args(), name))

    def scale(self, name: str, count: int, wait: bool = True) -> None:
        pool = self.client.agent_pools.get(*self._args(), name)
        model = self._to_model(pool)
        if model.protected:
            raise PermissionError(f"node pool {name} is protected")
        if count < model.min_count:
            raise ValueError(f"{model.mode} node pool {name} needs at least {model.min_count} node(s)")
        if pool.enable_auto_scaling:
            raise ValueError(f"node pool {name} uses the cluster autoscaler; change min/max instead")
        if _is_vms_pool(pool):
            manual = pool.virtual_machines_profile.scale.manual
            # Put the whole count on the first VM size, zero the rest
            for i, m in enumerate(manual):
                m.count = count if i == 0 else 0
        else:
            pool.count = count
        poller = self.client.agent_pools.begin_create_or_update(*self._args(), name, pool)
        if wait:
            poller.result()
