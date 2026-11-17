from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import kube as kube_module
from app.config import Settings
from app.kube import ArgoApp, Kube


class RecordingApps:
    def __init__(self, annotations):
        self.annotations = annotations
        self.patches = []

    def list_namespaced_deployment(self, ns):
        return SimpleNamespace(items=[SimpleNamespace(
            metadata=SimpleNamespace(name="api", annotations=self.annotations),
            spec=SimpleNamespace(replicas=0), status=SimpleNamespace(ready_replicas=0))])

    def list_namespaced_stateful_set(self, ns):
        return SimpleNamespace(items=[])

    def patch_namespaced_deployment(self, name, ns, body, field_manager):
        self.patches.append(body)


class RecordingCustom:
    def __init__(self, items=()):
        self.items = list(items)
        self.patches = []
        self.selectors = []

    def list_cluster_custom_object(self, group, version, plural, label_selector=""):
        self.selectors.append((group, plural, label_selector))
        return {"items": self.items}

    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body, field_manager):
        self.patches.append((plural, ns, name, body))


@pytest.fixture
def make_kube(monkeypatch):
    monkeypatch.setattr(kube_module.config, "load_incluster_config", lambda: None)

    def make(prefix, annotations=None):
        k = Kube(prefix)
        k.apps = RecordingApps(annotations or {})
        return k
    return make


def test_annotations_use_configured_prefix(make_kube):
    k = make_kube("sleep.example.com", {"sleep.example.com/original-replicas": "3",
                                        "kube-siesta.io/original-replicas": "9"})
    [w] = k.list_workloads("dev1")
    assert w.original_replicas == 3
    k.scale_workload(w, 0, original=3)
    assert k.apps.patches[-1]["metadata"]["annotations"] == {"sleep.example.com/original-replicas": "3"}


def test_default_prefix():
    assert Settings().label_prefix == "kube-siesta.io"


@pytest.mark.parametrize("prefix", ["sleep.example.com", "kube-siesta", "a1.b-2.c"])
def test_valid_prefix(monkeypatch, prefix):
    monkeypatch.setenv("SIESTA_LABEL_PREFIX", prefix)
    assert Settings().label_prefix == prefix


@pytest.mark.parametrize("prefix", ["", "Example.com", "example.com/", "-example.com", "example..com", "a_b.com"])
def test_invalid_prefix_rejected(monkeypatch, prefix):
    monkeypatch.setenv("SIESTA_LABEL_PREFIX", prefix)
    with pytest.raises(ValidationError, match="label_prefix"):
        Settings()


def test_argocd_apps_listed_by_label_across_namespaces(make_kube):
    k = make_kube("sleep.example.com")
    k.custom = RecordingCustom([
        {"metadata": {"namespace": "team-a", "name": "b"}, "spec": {"syncPolicy": {"automated": {"prune": True}}}},
        {"metadata": {"namespace": "argocd", "name": "a",
                      "annotations": {"sleep.example.com/auto-sync": '{"selfHeal": true}'}}, "spec": {}},
    ])
    apps = k.argocd_apps("dev1")
    assert k.custom.selectors == [("argoproj.io", "applications", "sleep.example.com/namespace=dev1")]
    assert [(a.namespace, a.name, a.auto_sync, a.paused_by_us) for a in apps] == [
        ("argocd", "a", False, True), ("team-a", "b", True, False)]


def test_argocd_pause_saves_policy_and_resume_restores_it(make_kube):
    k = make_kube("sleep.example.com")
    k.custom = RecordingCustom()
    k.pause_argocd_app(ArgoApp("argocd", "dev1", {"prune": True, "selfHeal": True}, None))
    _, ns, name, body = k.custom.patches[-1]
    assert (ns, name) == ("argocd", "dev1")
    assert body["spec"] == {"syncPolicy": {"automated": None}}
    saved = body["metadata"]["annotations"]["sleep.example.com/auto-sync"]

    k.resume_argocd_app(ArgoApp("argocd", "dev1", None, saved))
    _, _, _, body = k.custom.patches[-1]
    assert body["spec"] == {"syncPolicy": {"automated": {"prune": True, "selfHeal": True}}}
    assert body["metadata"]["annotations"] == {"sleep.example.com/auto-sync": None,
                                               "argocd.argoproj.io/refresh": "normal"}
