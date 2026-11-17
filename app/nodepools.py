"""Node pool model shared by the AKS and EKS backends, and the factory that picks one.

A backend provides list() -> list[NodePool], get(name) -> NodePool and scale(name, count, wait).
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from app.config import get_settings


@dataclass
class NodePool:
    name: str
    mode: str  # System | User
    vm_size: str
    count: int
    autoscaling: bool
    provisioning_state: str
    protected: bool

    @property
    def is_system(self) -> bool:
        return self.mode == "System"

    @property
    def min_count(self) -> int:
        return 1 if self.is_system else 0


class NodePools(Protocol):
    def list(self) -> list[NodePool]: ...

    def get(self, name: str) -> NodePool: ...

    def scale(self, name: str, count: int, wait: bool = True) -> None: ...


@lru_cache
def get_nodepools() -> NodePools:
    settings = get_settings()
    if settings.provider == "eks":
        from app.eks import EksNodePools
        return EksNodePools(settings)
    from app.aks import AksNodePools
    return AksNodePools(settings)
