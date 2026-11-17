"""Sleep/wake orchestration shared by the web UI and the in-process scheduler.

Sleeping a namespace:  suspend its Flux objects and Argo CD auto-sync -> save replicas & scale to 0
                       -> suspend CronJobs -> pause KEDA.
Waking a namespace:    ensure user node pools have nodes -> restore replicas -> resume CronJobs -> resume KEDA
                       -> resume Flux and Argo CD auto-sync.
GitOps is suspended first so it cannot put replicas back while we work, and resumed last so the
sync it triggers starts from the restored state. Flux, Argo CD and KEDA are each optional
(SIESTA_FLUX_ENABLED, SIESTA_ARGOCD_ENABLED, SIESTA_KEDA_ENABLED); with none, only replicas and CronJobs are touched.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from app.config import Settings
from app.kube import ArgoApp, FluxObject, Kube, KedaObject, Workload, matches_any
from app.nodepools import NodePool, NodePools
from app.state import State

log = logging.getLogger(__name__)


@dataclass
class NamespaceStatus:
    name: str
    managed: bool       # included in the scheduled scale-down
    protected: bool     # UI must not modify
    workloads: list[Workload] = field(default_factory=list)
    flux: list[FluxObject] = field(default_factory=list)
    argocd: list[ArgoApp] = field(default_factory=list)
    keda: list[KedaObject] = field(default_factory=list)
    lease_until: str | None = None
    owner: str = ""

    @property
    def running(self) -> int:
        return sum(1 for w in self.workloads if w.replicas > 0)

    @property
    def flux_isolated(self) -> bool:
        """True when a Kustomization is labelled for this namespace, so suspending it won't touch others."""
        return any(f.kind == "Kustomization" for f in self.flux)

    @property
    def argocd_isolated(self) -> bool:
        """True when an Application is labelled for this namespace."""
        return bool(self.argocd)

    @property
    def state(self) -> str:
        if not self.workloads:
            return "empty"
        if (any(f.suspended_by_us for f in self.flux) or any(a.paused_by_us for a in self.argocd)
                or any(k.paused_by_us for k in self.keda)
                or any(w.original_replicas is not None for w in self.workloads)):
            return "asleep" if self.running == 0 else "partial"
        return "awake" if self.running else "asleep"


class Scaler:
    def __init__(self, kube: Kube, pools: NodePools, state: State, settings: Settings) -> None:
        self.kube = kube
        self.pools = pools
        self.state = state
        self.settings = settings

    # --- queries ------------------------------------------------------------

    def is_managed(self, ns: str) -> bool:
        return matches_any(ns, self.settings.managed_namespaces)

    def is_protected(self, ns: str) -> bool:
        # Flux and Argo CD themselves are never put to sleep, whether or not they are listed
        if self.settings.flux_enabled and ns == self.settings.flux_namespace:
            return True
        if self.settings.argocd_enabled and ns == self.settings.argocd_namespace:
            return True
        return matches_any(ns, self.settings.protected_namespaces)

    def managed_namespaces(self) -> list[str]:
        return [ns for ns in self.kube.list_namespaces() if self.is_managed(ns) and not self.is_protected(ns)]

    def namespace_status(self, ns: str, leases: dict | None = None, owners: dict | None = None) -> NamespaceStatus:
        leases = self.state.active_leases() if leases is None else leases
        owners = self.state.owners() if owners is None else owners
        return NamespaceStatus(
            name=ns,
            managed=self.is_managed(ns),
            protected=self.is_protected(ns),
            workloads=self.kube.list_workloads(ns),
            flux=self._flux_objects(ns),
            argocd=self._argocd_apps(ns),
            keda=self._keda_objects(ns),
            lease_until=leases.get(ns, {}).get("until"),
            owner=owners.get(ns, ""),
        )

    def _check_modifiable(self, ns: str) -> None:
        if self.is_protected(ns):
            raise PermissionError(f"namespace {ns} is protected")

    # --- flux / argo cd / keda ---------------------------------------------

    def _flux_objects(self, ns: str) -> list[FluxObject]:
        return self.kube.flux_objects(ns, self.settings.flux_namespace) if self.settings.flux_enabled else []

    def _argocd_apps(self, ns: str) -> list[ArgoApp]:
        return self.kube.argocd_apps(ns) if self.settings.argocd_enabled else []

    def _keda_objects(self, ns: str) -> list[KedaObject]:
        return self.kube.keda_objects(ns) if self.settings.keda_enabled else []

    def _suspend_flux(self, ns: str) -> None:
        for obj in self._flux_objects(ns):
            if not obj.suspended:
                self.kube.set_flux_suspended(obj, True)

    def _resume_flux(self, ns: str) -> None:
        objs = self._flux_objects(ns)
        # HelmReleases first, then the Kustomization that owns them
        for obj in sorted(objs, key=lambda o: o.kind == "Kustomization"):
            if obj.suspended_by_us:
                self.kube.set_flux_suspended(obj, False)

    def _pause_argocd(self, ns: str) -> None:
        for app in self._argocd_apps(ns):
            if app.auto_sync:
                self.kube.pause_argocd_app(app)

    def _resume_argocd(self, ns: str) -> None:
        for app in self._argocd_apps(ns):
            if app.paused_by_us:
                self.kube.resume_argocd_app(app)

    def _pause_keda(self, ns: str) -> None:
        for obj in self._keda_objects(ns):
            if not obj.paused:
                self.kube.set_keda_paused(obj, True)

    def _resume_keda(self, ns: str) -> None:
        for obj in self._keda_objects(ns):
            if obj.paused_by_us:
                self.kube.set_keda_paused(obj, False)

    # --- namespace sleep / wake --------------------------------------------

    def sleep_namespace(self, ns: str, actor: str) -> None:
        self._check_modifiable(ns)
        flux = self._flux_objects(ns)
        if (self.settings.flux_enabled and self.settings.require_flux_isolation
                and not any(f.kind == "Kustomization" for f in flux)):
            raise RuntimeError(
                f"no Flux Kustomization labelled {self.settings.label_prefix}/namespace={ns}; Flux would revert the scale-down")
        if (self.settings.argocd_enabled and self.settings.require_argocd_isolation
                and not self._argocd_apps(ns)):
            raise RuntimeError(
                f"no Argo CD Application labelled {self.settings.label_prefix}/namespace={ns}; Argo CD would revert the scale-down")
        self._suspend_flux(ns)
        self._pause_argocd(ns)
        self._pause_keda(ns)
        for w in self.kube.list_workloads(ns):
            if w.replicas > 0:
                # Keep the first saved value if a workload was partially woken and is going back to sleep
                original = w.original_replicas if w.original_replicas is not None else w.replicas
                self.kube.scale_workload(w, 0, original=original)
        self.kube.suspend_cronjobs(ns)
        self.state.delete_lease(ns)
        self.state.audit(actor, "sleep", ns)
        log.info("namespace %s asleep", ns)

    def wake_namespace(self, ns: str, actor: str, ensure_nodes: bool = True) -> None:
        self._check_modifiable(ns)
        if ensure_nodes:
            self.ensure_nodes(actor)
        for w in self.kube.list_workloads(ns):
            if w.original_replicas is not None:
                self.kube.scale_workload(w, w.original_replicas, original=None)
        self.kube.resume_cronjobs(ns)
        self._resume_keda(ns)
        self._resume_flux(ns)
        self._resume_argocd(ns)
        if self.settings.in_off_window():
            self._set_lease(ns, actor)
        self.state.audit(actor, "wake", ns)
        log.info("namespace %s awake", ns)

    def set_owner(self, ns: str, owner: str, actor: str) -> None:
        """Record who to ask about a namespace. Empty clears it."""
        self._check_modifiable(ns)
        owner = " ".join(owner.split())[:64]
        self.state.set_owner(ns, owner, actor)
        self.state.audit(actor, "set-owner", ns, owner or "(cleared)")

    def extend_lease(self, ns: str, actor: str) -> None:
        self._check_modifiable(ns)
        self._set_lease(ns, actor)
        self.state.audit(actor, "extend", ns, f"{self.settings.lease_hours}h")

    def _set_lease(self, ns: str, actor: str) -> None:
        until = (self.settings.now() + timedelta(hours=self.settings.lease_hours)).isoformat(timespec="seconds")
        self.state.set_lease(ns, until, actor)

    # --- single workload ----------------------------------------------------

    def scale_workload(self, ns: str, kind: str, name: str, replicas: int, actor: str) -> None:
        """Scale one Deployment/StatefulSet. Flux, Argo CD and KEDA for the namespace are suspended so it is not
        reverted; waking the namespace later restores every workload and resumes them."""
        self._check_modifiable(ns)
        w = next((w for w in self.kube.list_workloads(ns) if w.kind == kind and w.name == name), None)
        if w is None:
            raise LookupError(f"{kind} {ns}/{name} not found")
        if replicas > 0:
            self.ensure_nodes(actor)
        self._suspend_flux(ns)
        self._pause_argocd(ns)
        self._pause_keda(ns)
        original = w.original_replicas if w.original_replicas is not None else w.replicas
        self.kube.scale_workload(w, replicas, original=original)
        if replicas > 0 and self.settings.in_off_window():
            self._set_lease(ns, actor)
        self.state.audit(actor, "scale", f"{kind}/{ns}/{name}", f"{w.replicas} -> {replicas}")

    # --- node pools ---------------------------------------------------------

    def scale_pool(self, name: str, count: int, actor: str, overwrite_saved: bool | None = None) -> None:
        """Scale a node pool, remembering its previous size when shrinking so it can be restored.

        During the off window the size saved by the scheduled scale-down is kept, so a developer
        scaling up and back down at night or on Saturday doesn't change what the morning restores.
        """
        pool = self.pools.get(name)
        if count < pool.min_count:
            raise ValueError(f"{pool.mode} node pool {name} needs at least {pool.min_count} node(s)")
        if overwrite_saved is None:
            overwrite_saved = not self.settings.in_off_window()
        if count < pool.count:
            self.state.save_pool_count(name, pool.count, overwrite=overwrite_saved)
        self.state.audit(actor, "scale-pool", name, f"{pool.count} -> {count}")
        # Shrinking a system pool drains the node this process may be running on, so don't wait for it
        self.pools.scale(name, count, wait=not (pool.is_system and count < pool.count))

    def weekend_count(self, pool: NodePool) -> int | None:
        """Target size during the off window; None means the pool is not scaled."""
        if pool.protected or pool.autoscaling:
            return None
        default = None if pool.is_system else 0
        return self.settings.weekend_node_counts.get(pool.name, default)

    def ensure_nodes(self, actor: str) -> None:
        """Bring every user node pool that is at 0 back to its saved count."""
        saved = self.state.pool_counts()
        for pool in self.pools.list():
            if not pool.is_system and self.weekend_count(pool) is not None and pool.count == 0:
                self.scale_pool(pool.name, saved.get(pool.name, self.settings.default_wake_node_count), actor)

    def sleep_pools(self, actor: str, overwrite_saved: bool | None = None) -> None:
        # User pools first: their pods move to the system pool, which is shrunk last
        for pool in sorted(self.pools.list(), key=lambda p: p.is_system):
            target = self.weekend_count(pool)
            if target is not None and pool.count > target:
                self.scale_pool(pool.name, target, actor, overwrite_saved)

    def restore_pools(self, actor: str) -> None:
        """Return every scaled pool to its saved size, system pools first."""
        saved = self.state.pool_counts()
        for pool in sorted(self.pools.list(), key=lambda p: not p.is_system):
            if self.weekend_count(pool) is None:
                continue
            target = saved.get(pool.name, self.settings.default_wake_node_count if pool.count == 0 else pool.count)
            if pool.count < target:
                self.scale_pool(pool.name, target, actor)

    # --- scheduled jobs -----------------------------------------------------

    def run_schedule(self) -> None:
        """Called every few minutes by the in-process scheduler. Scales everything down once when an off
        window starts and back up once when it ends, then expires weekend/night leases.

        Each window is identified by its start time. Scale-up only happens for a window we
        scaled down ourselves, so namespaces put to sleep by hand aren't woken by a morning run.
        """
        now = self.settings.now()
        done = self.state.schedule_marks()
        window = self.settings.current_off_window(now)
        if window:
            start = window[0].isoformat()
            if done.get("down") != start:
                log.info("off window %s -> %s started; scaling down", *window)
                self.scale_down_all()
                self.state.set_schedule_mark("down", start)
            self.expire_leases()
            return
        previous = self.settings.previous_off_window(now)
        if previous:
            start = previous[0].isoformat()
            if done.get("down") == start and done.get("up") != start:
                log.info("off window %s -> %s ended; scaling up", *previous)
                self.scale_up_all()
                self.state.set_schedule_mark("up", start)
                return
        log.info("working hours; nothing to do")

    def scale_down_all(self) -> None:
        actor = "schedule"
        failures = []
        for ns in self.managed_namespaces():
            try:
                self.sleep_namespace(ns, actor)
            except Exception:
                log.exception("failed to sleep %s", ns)
                failures.append(ns)
        self.state.clear_leases()
        if failures:
            # Don't pull nodes out from under workloads we failed to stop
            raise RuntimeError(f"left node pools up; failed namespaces: {failures}")
        # Working-hours sizes are the ones to restore when the off window ends
        self.sleep_pools(actor, overwrite_saved=True)

    def scale_up_all(self) -> None:
        actor = "schedule"
        self.restore_pools(actor)
        failures = []
        for ns in self.managed_namespaces():
            try:
                self.wake_namespace(ns, actor, ensure_nodes=False)
            except Exception:
                log.exception("failed to wake %s", ns)
                failures.append(ns)
        self.state.clear_leases()
        if failures:
            raise RuntimeError(f"failed to wake: {failures}")

    def expire_leases(self) -> None:
        """During the off window, put namespaces with expired leases back to sleep,
        and drop the node pools once nothing is awake."""
        if not self.settings.in_off_window():
            return
        actor = "lease-expiry"
        active = self.state.active_leases()
        for ns in self.state.leases():
            if ns not in active:
                self.sleep_namespace(ns, actor)
        # Anything still running in a managed namespace without a lease also goes back to sleep
        for ns in self.managed_namespaces():
            if ns not in active and self.namespace_status(ns, active).running:
                self.sleep_namespace(ns, actor)
        # Any active lease (managed namespace or not) keeps the node pools up
        if not active:
            self.sleep_pools(actor)
