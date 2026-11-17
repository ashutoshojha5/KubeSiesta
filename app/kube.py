"""Thin wrapper over the Kubernetes API for the objects this app touches."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from functools import lru_cache

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

FIELD_MANAGER = "kube-siesta"

FLUX_KUSTOMIZATION = ("kustomize.toolkit.fluxcd.io", "v1", "kustomizations")
FLUX_HELMRELEASE = ("helm.toolkit.fluxcd.io", "v2", "helmreleases")

ARGOCD_APPLICATION = ("argoproj.io", "v1alpha1", "applications")

KEDA_SCALEDOBJECT = ("keda.sh", "v1alpha1", "scaledobjects")
KEDA_SCALEDJOB = ("keda.sh", "v1alpha1", "scaledjobs")
KEDA_PAUSED = "autoscaling.keda.sh/paused"


@dataclass
class Workload:
    kind: str  # Deployment | StatefulSet
    namespace: str
    name: str
    replicas: int
    ready: int
    original_replicas: int | None  # set while scaled down by us


@dataclass
class FluxObject:
    kind: str  # Kustomization | HelmRelease
    namespace: str
    name: str
    suspended: bool
    suspended_by_us: bool


@dataclass
class ArgoApp:
    namespace: str
    name: str
    automated: dict | None   # spec.syncPolicy.automated; None when auto-sync is off
    saved: str | None        # policy saved by us when we turned auto-sync off (JSON)

    @property
    def auto_sync(self) -> bool:
        return self.automated is not None

    @property
    def paused_by_us(self) -> bool:
        return self.saved is not None


@dataclass
class KedaObject:
    kind: str  # ScaledObject | ScaledJob
    namespace: str
    name: str
    paused: bool
    paused_by_us: bool


def matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch(name, p) for p in patterns)


class Kube:
    def __init__(self, label_prefix: str) -> None:
        self.original_replicas = f"{label_prefix}/original-replicas"
        self.suspended_by_us = f"{label_prefix}/suspended"
        self.namespace_label = f"{label_prefix}/namespace"
        self.saved_auto_sync = f"{label_prefix}/auto-sync"
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.batch = client.BatchV1Api()
        self.custom = client.CustomObjectsApi()

    # --- namespaces ---------------------------------------------------------

    def list_namespaces(self) -> list[str]:
        return sorted(ns.metadata.name for ns in self.core.list_namespace().items)

    # --- deployments / statefulsets ----------------------------------------

    def list_workloads(self, namespace: str) -> list[Workload]:
        result = []
        for kind, items in (
            ("Deployment", self.apps.list_namespaced_deployment(namespace).items),
            ("StatefulSet", self.apps.list_namespaced_stateful_set(namespace).items),
        ):
            for obj in items:
                original = (obj.metadata.annotations or {}).get(self.original_replicas)
                result.append(Workload(
                    kind=kind,
                    namespace=namespace,
                    name=obj.metadata.name,
                    replicas=obj.spec.replicas or 0,
                    ready=obj.status.ready_replicas or 0,
                    original_replicas=int(original) if original is not None else None,
                ))
        return sorted(result, key=lambda w: (w.kind, w.name))

    def scale_workload(self, w: Workload, replicas: int, original: int | None | str = "keep") -> None:
        """Set replicas; `original` sets/clears the saved-replicas annotation ("keep" leaves it)."""
        body: dict = {"spec": {"replicas": replicas}}
        if original != "keep":
            body["metadata"] = {"annotations": {self.original_replicas: None if original is None else str(original)}}
        patch = (self.apps.patch_namespaced_deployment if w.kind == "Deployment"
                 else self.apps.patch_namespaced_stateful_set)
        patch(w.name, w.namespace, body, field_manager=FIELD_MANAGER)

    # --- cronjobs -----------------------------------------------------------

    def suspend_cronjobs(self, namespace: str) -> None:
        for cj in self.batch.list_namespaced_cron_job(namespace).items:
            if not cj.spec.suspend:
                self.batch.patch_namespaced_cron_job(cj.metadata.name, namespace, {
                    "metadata": {"annotations": {self.suspended_by_us: "true"}},
                    "spec": {"suspend": True},
                }, field_manager=FIELD_MANAGER)

    def resume_cronjobs(self, namespace: str) -> None:
        for cj in self.batch.list_namespaced_cron_job(namespace).items:
            if (cj.metadata.annotations or {}).get(self.suspended_by_us):
                self.batch.patch_namespaced_cron_job(cj.metadata.name, namespace, {
                    "metadata": {"annotations": {self.suspended_by_us: None}},
                    "spec": {"suspend": False},
                }, field_manager=FIELD_MANAGER)

    # --- flux ---------------------------------------------------------------

    def _list_custom(self, crd: tuple[str, str, str], namespace: str, label_selector: str = "") -> list[dict]:
        group, version, plural = crd
        try:
            return self.custom.list_namespaced_custom_object(
                group, version, namespace, plural, label_selector=label_selector)["items"]
        except ApiException as e:
            if e.status == 404:  # CRD not installed
                return []
            raise

    def flux_objects(self, namespace: str, flux_namespace: str) -> list[FluxObject]:
        """Kustomizations labelled as owning `namespace`, plus the HelmReleases inside it."""
        objs = [("Kustomization", FLUX_KUSTOMIZATION, o) for o in
                self._list_custom(FLUX_KUSTOMIZATION, flux_namespace, f"{self.namespace_label}={namespace}")]
        objs += [("HelmRelease", FLUX_HELMRELEASE, o) for o in self._list_custom(FLUX_HELMRELEASE, namespace)]
        return [FluxObject(
            kind=kind,
            namespace=o["metadata"]["namespace"],
            name=o["metadata"]["name"],
            suspended=bool(o.get("spec", {}).get("suspend")),
            suspended_by_us=bool((o["metadata"].get("annotations") or {}).get(self.suspended_by_us)),
        ) for kind, _, o in objs]

    def set_flux_suspended(self, obj: FluxObject, suspend: bool) -> None:
        group, version, plural = FLUX_KUSTOMIZATION if obj.kind == "Kustomization" else FLUX_HELMRELEASE
        annotations: dict = {self.suspended_by_us: "true" if suspend else None}
        if not suspend:
            # Ask Flux to reconcile right away rather than waiting for the interval
            annotations["reconcile.fluxcd.io/requestedAt"] = datetime.now(timezone.utc).isoformat()
        self.custom.patch_namespaced_custom_object(
            group, version, obj.namespace, plural, obj.name,
            {"metadata": {"annotations": annotations}, "spec": {"suspend": suspend}},
            field_manager=FIELD_MANAGER,
        )

    # --- argo cd ------------------------------------------------------------

    def argocd_apps(self, namespace: str) -> list[ArgoApp]:
        """Applications labelled as owning `namespace`, in any namespace (Argo CD's apps-in-any-namespace)."""
        group, version, plural = ARGOCD_APPLICATION
        try:
            items = self.custom.list_cluster_custom_object(
                group, version, plural, label_selector=f"{self.namespace_label}={namespace}")["items"]
        except ApiException as e:
            if e.status == 404:  # CRD not installed
                return []
            raise
        return sorted((ArgoApp(
            namespace=o["metadata"]["namespace"],
            name=o["metadata"]["name"],
            automated=(o.get("spec", {}).get("syncPolicy") or {}).get("automated"),
            saved=(o["metadata"].get("annotations") or {}).get(self.saved_auto_sync),
        ) for o in items), key=lambda a: (a.namespace, a.name))

    def pause_argocd_app(self, app: ArgoApp) -> None:
        """Turn off automated sync, saving the policy so resume can put it back exactly. If a parent
        app put auto-sync back while we had it off, the policy saved first is kept."""
        group, version, plural = ARGOCD_APPLICATION
        saved = app.saved if app.saved is not None else json.dumps(app.automated)
        self.custom.patch_namespaced_custom_object(
            group, version, app.namespace, plural, app.name,
            {"metadata": {"annotations": {self.saved_auto_sync: saved}},
             "spec": {"syncPolicy": {"automated": None}}},
            field_manager=FIELD_MANAGER,
        )

    def resume_argocd_app(self, app: ArgoApp) -> None:
        """Restore the saved automated sync policy and ask Argo CD to refresh right away."""
        group, version, plural = ARGOCD_APPLICATION
        self.custom.patch_namespaced_custom_object(
            group, version, app.namespace, plural, app.name,
            {"metadata": {"annotations": {self.saved_auto_sync: None, "argocd.argoproj.io/refresh": "normal"}},
             "spec": {"syncPolicy": {"automated": json.loads(app.saved)}}},
            field_manager=FIELD_MANAGER,
        )

    # --- keda ---------------------------------------------------------------

    def keda_objects(self, namespace: str) -> list[KedaObject]:
        objs = [("ScaledObject", o) for o in self._list_custom(KEDA_SCALEDOBJECT, namespace)]
        objs += [("ScaledJob", o) for o in self._list_custom(KEDA_SCALEDJOB, namespace)]
        return [KedaObject(
            kind=kind,
            namespace=namespace,
            name=o["metadata"]["name"],
            paused=(o["metadata"].get("annotations") or {}).get(KEDA_PAUSED) == "true",
            paused_by_us=bool((o["metadata"].get("annotations") or {}).get(self.suspended_by_us)),
        ) for kind, o in objs]

    def set_keda_paused(self, obj: KedaObject, paused: bool) -> None:
        """Pause or resume. Resuming removes the pause annotation rather than setting it to "false"."""
        group, version, plural = KEDA_SCALEDOBJECT if obj.kind == "ScaledObject" else KEDA_SCALEDJOB
        self.custom.patch_namespaced_custom_object(
            group, version, obj.namespace, plural, obj.name,
            {"metadata": {"annotations": {
                KEDA_PAUSED: "true" if paused else None,
                self.suspended_by_us: "true" if paused else None,
            }}},
            field_manager=FIELD_MANAGER,
        )


@lru_cache
def get_kube(label_prefix: str) -> Kube:
    return Kube(label_prefix)
