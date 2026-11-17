import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.kube import ArgoApp, FluxObject, KedaObject, Workload
from app.nodepools import NodePool
from app.scaler import Scaler
from app.state import State

IST = ZoneInfo("Asia/Kolkata")


# --- fakes ------------------------------------------------------------------

class FakeKube:
    def __init__(self, workloads, flux=(), namespaces=None):
        self.workloads = {(w.namespace, w.kind, w.name): w for w in workloads}
        self.flux = {(f.kind, f.name): f for f in flux}
        self.namespaces = namespaces or sorted({w.namespace for w in workloads})
        self.cronjobs_suspended: set[str] = set()
        self.keda: dict[str, KedaObject] = {}
        self.argocd: dict[str, tuple[str, ArgoApp]] = {}  # name -> (namespace it is labelled for, app)
        self.argocd_calls: list[tuple[str, str]] = []

    def list_namespaces(self):
        return self.namespaces

    def list_workloads(self, ns):
        return [Workload(**vars(w)) for w in self.workloads.values() if w.namespace == ns]

    def scale_workload(self, w, replicas, original="keep"):
        stored = self.workloads[(w.namespace, w.kind, w.name)]
        stored.replicas = replicas
        if original != "keep":
            stored.original_replicas = original

    def suspend_cronjobs(self, ns):
        self.cronjobs_suspended.add(ns)

    def resume_cronjobs(self, ns):
        self.cronjobs_suspended.discard(ns)

    def keda_objects(self, ns):
        return [KedaObject(**vars(k)) for k in self.keda.values() if k.namespace == ns]

    def set_keda_paused(self, obj, paused):
        k = self.keda[obj.name]
        k.paused = k.paused_by_us = paused

    def argocd_apps(self, ns):
        return [ArgoApp(**vars(a)) for owner, a in self.argocd.values() if owner == ns]

    def pause_argocd_app(self, app):
        stored = self.argocd[app.name][1]
        stored.saved = app.saved if app.saved is not None else json.dumps(app.automated)
        stored.automated = None
        self.argocd_calls.append(("pause", app.name))

    def resume_argocd_app(self, app):
        stored = self.argocd[app.name][1]
        stored.automated, stored.saved = json.loads(app.saved), None
        self.argocd_calls.append(("resume", app.name))

    def flux_objects(self, ns, flux_ns):
        return [FluxObject(**vars(f)) for f in self.flux.values() if ns in (f.namespace, f.name.removeprefix("apps-"))]

    def set_flux_suspended(self, obj, suspend):
        f = self.flux[(obj.kind, obj.name)]
        f.suspended = suspend
        f.suspended_by_us = suspend


class FakePools:
    def __init__(self, pools):
        self.pools = {p.name: p for p in pools}
        self.calls = []  # (name, count, wait)

    def list(self):
        return list(self.pools.values())

    def get(self, name):
        return self.pools[name]

    def scale(self, name, count, wait=True):
        self.calls.append((name, count, wait))
        self.pools[name].count = count


class FixedClockSettings(Settings):
    fixed_now: datetime = datetime(2026, 10, 21, 12, 0, tzinfo=IST)  # Wednesday

    def now(self):
        return self.fixed_now


def wl(ns, name, replicas, kind="Deployment"):
    return Workload(kind=kind, namespace=ns, name=name, replicas=replicas, ready=replicas, original_replicas=None)


@pytest.fixture
def setup():
    settings = FixedClockSettings()
    kube = FakeKube(
        [wl("dev1", "api", 2), wl("dev1", "mongo", 1, "StatefulSet"), wl("dev1", "idle", 0),
         wl("monitoring", "grafana", 1), wl("kube-system", "coredns", 2)],
        flux=[
            FluxObject("Kustomization", "flux-system", "apps-dev1", False, False),
            FluxObject("HelmRelease", "dev1", "api", False, False),
            FluxObject("Kustomization", "flux-system", "monitoring", False, False),
        ],
    )
    pools = FakePools([
        NodePool("systempool", "System", "D2", 4, False, "Succeeded", False),
        NodePool("userpool", "User", "D4", 3, False, "Succeeded", False),
    ])
    scaler = Scaler(kube, pools, State(settings), settings)
    return settings, kube, pools, scaler


# --- tests --------------------------------------------------------------------

@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 10, 23, 20, 59, tzinfo=IST), False),  # Fri before 21:00
    (datetime(2026, 10, 23, 21, 0, tzinfo=IST), True),    # Fri 21:00
    (datetime(2026, 10, 24, 12, 0, tzinfo=IST), True),    # Sat
    (datetime(2026, 10, 25, 23, 59, tzinfo=IST), True),   # Sun night
    (datetime(2026, 10, 26, 6, 59, tzinfo=IST), True),    # Mon early
    (datetime(2026, 10, 26, 7, 0, tzinfo=IST), False),    # Mon 07:00
    (datetime(2026, 10, 21, 12, 0, tzinfo=IST), False),   # Wed
])
def test_off_window(when, expected):
    assert Settings().in_off_window(when) is expected


def test_off_window_custom_hours():
    s = Settings(weekend_start_day="sat", weekend_start_time=time(0, 0), weekend_end_day="mon", weekend_end_time=time(8, 0))
    assert not s.in_off_window(datetime(2026, 10, 23, 23, 0, tzinfo=IST))
    assert s.in_off_window(datetime(2026, 10, 26, 7, 59, tzinfo=IST))


def test_managed_namespaces(setup):
    _, kube, _, scaler = setup
    kube.namespaces = ["staging", "default", "dev1", "dev4", "devtools-x", "kube-system", "monitoring", "ml-models", "prod"]
    assert scaler.managed_namespaces() == ["staging", "dev1", "dev4", "devtools-x", "monitoring", "ml-models"]


def test_sleep_then_wake_restores_replicas(setup):
    _, kube, _, scaler = setup
    scaler.sleep_namespace("dev1", "alice")
    w = kube.workloads
    assert [w[("dev1", "Deployment", "api")].replicas, w[("dev1", "StatefulSet", "mongo")].replicas] == [0, 0]
    assert w[("dev1", "Deployment", "api")].original_replicas == 2
    assert w[("dev1", "Deployment", "idle")].original_replicas is None  # was already 0
    assert all(f.suspended for f in kube.flux_objects("dev1", ""))
    assert "dev1" in kube.cronjobs_suspended
    assert scaler.namespace_status("dev1").state == "asleep"

    scaler.wake_namespace("dev1", "alice")
    assert w[("dev1", "Deployment", "api")].replicas == 2
    assert w[("dev1", "StatefulSet", "mongo")].replicas == 1
    assert w[("dev1", "Deployment", "idle")].replicas == 0
    assert all(wl.original_replicas is None for wl in w.values())
    assert not any(f.suspended for f in kube.flux_objects("dev1", ""))
    assert "dev1" not in kube.cronjobs_suspended


def test_wake_does_not_resume_flux_suspended_by_someone_else(setup):
    _, kube, _, scaler = setup
    kube.flux[("Kustomization", "apps-dev1")].suspended = True  # manual `flux suspend`
    scaler.sleep_namespace("dev1", "alice")
    scaler.wake_namespace("dev1", "alice")
    assert kube.flux[("Kustomization", "apps-dev1")].suspended


def test_sleep_twice_keeps_original_replicas(setup):
    _, kube, _, scaler = setup
    scaler.sleep_namespace("dev1", "a")
    scaler.scale_workload("dev1", "Deployment", "api", 1, "a")  # partially woken
    scaler.sleep_namespace("dev1", "a")
    assert kube.workloads[("dev1", "Deployment", "api")].original_replicas == 2
    scaler.wake_namespace("dev1", "a")
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 2


def test_protected_namespace_refused(setup):
    _, _, _, scaler = setup
    with pytest.raises(PermissionError):
        scaler.sleep_namespace("kube-system", "a")


def test_flux_namespace_always_protected_when_flux_enabled(setup):
    settings, _, _, scaler = setup
    settings.protected_namespaces = []
    settings.managed_namespaces = ["*"]
    settings.flux_namespace = "gitops"
    assert scaler.is_protected("gitops")
    settings.flux_enabled = False
    assert not scaler.is_protected("gitops")


def test_sleep_refused_without_flux_isolation(setup):
    _, kube, _, scaler = setup
    del kube.flux[("Kustomization", "apps-dev1")]
    with pytest.raises(RuntimeError, match="Flux would revert"):
        scaler.sleep_namespace("dev1", "a")
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 2


def test_isolation_error_names_configured_label(setup, monkeypatch):
    monkeypatch.setenv("SIESTA_LABEL_PREFIX", "sleep.example.com")
    _, kube, pools, _ = setup
    settings = FixedClockSettings()
    del kube.flux[("Kustomization", "apps-dev1")]
    with pytest.raises(RuntimeError, match="sleep.example.com/namespace=dev1"):
        Scaler(kube, pools, State(settings), settings).sleep_namespace("dev1", "a")


def test_weekend_cycle(setup):
    settings, kube, pools, scaler = setup
    settings.fixed_now = datetime(2026, 10, 23, 21, 0, tzinfo=IST)
    scaler.scale_down_all()
    assert pools.get("userpool").count == 0
    assert pools.get("systempool").count == 1
    # User pool first and waited on; system pool last and not waited on (it may drain this pod)
    assert pools.calls == [("userpool", 0, True), ("systempool", 1, False)]
    assert kube.workloads[("monitoring", "Deployment", "grafana")].replicas == 0
    assert kube.workloads[("kube-system", "Deployment", "coredns")].replicas == 2
    assert scaler.state.pool_counts() == {"userpool": 3, "systempool": 4}

    # Saturday: a developer wakes dev1 -> nodes come back, lease is set
    settings.fixed_now = datetime(2026, 10, 24, 10, 0, tzinfo=IST)
    scaler.wake_namespace("dev1", "bob")
    assert pools.get("userpool").count == 3
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 2
    assert kube.workloads[("monitoring", "Deployment", "grafana")].replicas == 0
    assert "dev1" in scaler.state.active_leases()

    # Lease still valid: nothing changes
    settings.fixed_now = datetime(2026, 10, 24, 17, 0, tzinfo=IST)
    scaler.expire_leases()
    assert pools.get("userpool").count == 3

    # Lease expired: dev1 sleeps, pools go to 0
    settings.fixed_now = datetime(2026, 10, 24, 18, 1, tzinfo=IST)
    scaler.expire_leases()
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 0
    assert pools.get("userpool").count == 0

    # Monday morning
    settings.fixed_now = datetime(2026, 10, 26, 7, 0, tzinfo=IST)
    scaler.scale_up_all()
    assert pools.get("userpool").count == 3
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 2
    assert kube.workloads[("monitoring", "Deployment", "grafana")].replicas == 1


def test_expire_leases_noop_on_weekday(setup):
    _, kube, pools, scaler = setup
    scaler.expire_leases()
    assert pools.get("userpool").count == 3
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 2


def test_weekend_down_keeps_nodes_if_a_namespace_fails(setup):
    settings, kube, pools, scaler = setup
    settings.fixed_now = datetime(2026, 10, 23, 21, 0, tzinfo=IST)

    def boom(ns):
        if ns == "monitoring":
            raise RuntimeError("api error")
    kube.suspend_cronjobs = boom
    with pytest.raises(RuntimeError):
        scaler.scale_down_all()
    assert pools.get("userpool").count == 3


def test_weekend_up_restores_system_pool_first(setup):
    settings, _, pools, scaler = setup
    settings.fixed_now = datetime(2026, 10, 23, 21, 0, tzinfo=IST)
    scaler.scale_down_all()
    pools.calls.clear()
    settings.fixed_now = datetime(2026, 10, 26, 7, 0, tzinfo=IST)
    scaler.scale_up_all()
    assert pools.calls == [("systempool", 4, True), ("userpool", 3, True)]


def test_wake_on_weekend_leaves_system_pool_small(setup):
    settings, _, pools, scaler = setup
    settings.fixed_now = datetime(2026, 10, 23, 21, 0, tzinfo=IST)
    scaler.scale_down_all()
    settings.fixed_now = datetime(2026, 10, 24, 10, 0, tzinfo=IST)
    scaler.wake_namespace("dev1", "bob")
    assert pools.get("userpool").count == 3
    assert pools.get("systempool").count == 1


def test_weekend_manual_resize_does_not_change_monday_size(setup):
    settings, _, pools, scaler = setup
    settings.fixed_now = datetime(2026, 10, 23, 21, 0, tzinfo=IST)
    scaler.scale_down_all()
    settings.fixed_now = datetime(2026, 10, 24, 10, 0, tzinfo=IST)
    scaler.scale_pool("userpool", 2, "bob")
    scaler.scale_pool("userpool", 1, "bob")  # shrinking during the weekend must not overwrite the saved 3
    scaler.expire_leases()
    assert pools.get("userpool").count == 0
    settings.fixed_now = datetime(2026, 10, 26, 7, 0, tzinfo=IST)
    scaler.scale_up_all()
    assert pools.get("userpool").count == 3


def test_weekday_resize_updates_saved_size(setup):
    _, _, pools, scaler = setup
    scaler.scale_pool("userpool", 2, "ops")
    assert scaler.state.pool_counts()["userpool"] == 3
    scaler.scale_pool("userpool", 5, "ops")
    scaler.scale_pool("userpool", 4, "ops")
    assert scaler.state.pool_counts()["userpool"] == 5


def test_system_pool_cannot_go_to_zero(setup):
    _, _, pools, scaler = setup
    with pytest.raises(ValueError, match="at least 1"):
        scaler.scale_pool("systempool", 0, "a")
    assert pools.get("systempool").count == 4


def test_system_pool_not_in_weekend_counts_is_left_alone(setup):
    settings, _, pools, scaler = setup
    settings.weekend_node_counts = {}
    settings.fixed_now = datetime(2026, 10, 23, 21, 0, tzinfo=IST)
    scaler.scale_down_all()
    assert pools.get("systempool").count == 4
    assert pools.get("userpool").count == 0


def test_missing_setting_fails(monkeypatch):
    monkeypatch.delenv("SIESTA_AKS_CLUSTER_NAME")
    with pytest.raises(ValueError, match="SIESTA_AKS_CLUSTER_NAME"):
        Settings()


@pytest.mark.parametrize("key,value", [
    ("SIESTA_WEEKEND_START_DAY", "friday"),
    ("SIESTA_TIMEZONE", "Asia/Bangalore"),
    ("SIESTA_WEEKEND_NODE_COUNTS", "systempool=1"),
])
def test_invalid_setting_fails(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        Settings()


# --- nightly & schedule --------------------------------------------------------

@pytest.mark.parametrize("when,nightly,expected", [
    (datetime(2026, 10, 21, 21, 0, tzinfo=IST), True, True),     # Wed 21:00
    (datetime(2026, 10, 22, 3, 0, tzinfo=IST), True, True),      # Thu 03:00
    (datetime(2026, 10, 22, 7, 0, tzinfo=IST), True, False),     # Thu 07:00
    (datetime(2026, 10, 22, 12, 0, tzinfo=IST), True, False),    # Thu noon
    (datetime(2026, 10, 21, 23, 0, tzinfo=IST), False, False),   # Wed night, nightly off
    (datetime(2026, 10, 24, 12, 0, tzinfo=IST), False, True),    # Sat: weekend regardless
    (datetime(2026, 10, 24, 12, 0, tzinfo=IST), True, True),
])
def test_nightly_window(when, nightly, expected):
    assert Settings(nightly_enabled=nightly).in_off_window(when) is expected


def test_weekend_and_nightly_times_are_independent():
    s = Settings(nightly_enabled=True, nightly_start_time=time(22, 0), nightly_end_time=time(6, 0),
                 weekend_start_time=time(19, 0), weekend_end_time=time(9, 0))
    assert not s.in_off_window(datetime(2026, 10, 21, 21, 30, tzinfo=IST))  # Wed 21:30
    assert s.in_off_window(datetime(2026, 10, 21, 22, 0, tzinfo=IST))       # Wed 22:00
    assert not s.in_off_window(datetime(2026, 10, 22, 6, 0, tzinfo=IST))    # Thu 06:00
    assert s.in_off_window(datetime(2026, 10, 23, 19, 0, tzinfo=IST))       # Fri 19:00: weekend
    assert s.in_off_window(datetime(2026, 10, 26, 8, 59, tzinfo=IST))       # Mon 08:59
    assert not s.in_off_window(datetime(2026, 10, 26, 9, 0, tzinfo=IST))
    # Thursday night ends Fri 06:00 and Friday daytime is working hours
    assert not s.in_off_window(datetime(2026, 10, 23, 12, 0, tzinfo=IST))
    # Friday night and the weekend merge into one window
    assert s.current_off_window(datetime(2026, 10, 24, 3, 0, tzinfo=IST)) == (
        datetime(2026, 10, 23, 19, 0, tzinfo=IST), datetime(2026, 10, 26, 9, 0, tzinfo=IST))


def test_nightly_window_not_wrapping_midnight():
    s = Settings(nightly_enabled=True, nightly_start_time=time(1, 0), nightly_end_time=time(5, 0))
    assert s.in_off_window(datetime(2026, 10, 21, 3, 0, tzinfo=IST))
    assert not s.in_off_window(datetime(2026, 10, 21, 23, 0, tzinfo=IST))


@pytest.mark.parametrize("now,nightly,expected", [
    (datetime(2026, 10, 21, 22, 0, tzinfo=IST), True, datetime(2026, 10, 22, 7, 0, tzinfo=IST)),
    (datetime(2026, 10, 22, 3, 0, tzinfo=IST), True, datetime(2026, 10, 22, 7, 0, tzinfo=IST)),
    (datetime(2026, 10, 23, 22, 0, tzinfo=IST), True, datetime(2026, 10, 26, 7, 0, tzinfo=IST)),   # Fri night
    (datetime(2026, 10, 24, 10, 0, tzinfo=IST), False, datetime(2026, 10, 26, 7, 0, tzinfo=IST)),  # Sat
    (datetime(2026, 10, 22, 12, 0, tzinfo=IST), True, None),
])
def test_off_window_ends_at(now, nightly, expected):
    assert Settings(nightly_enabled=nightly).off_window_ends_at(now) == expected


def at(settings, scaler, when):
    settings.fixed_now = when
    scaler.run_schedule()


def api_replicas(kube):
    return kube.workloads[("dev1", "Deployment", "api")].replicas


def test_schedule_nightly_cycle(setup):
    settings, kube, pools, scaler = setup
    settings.nightly_enabled = True
    at(settings, scaler, datetime(2026, 10, 20, 20, 55, tzinfo=IST))  # Tue, before the window
    assert api_replicas(kube) == 2
    at(settings, scaler, datetime(2026, 10, 20, 21, 0, tzinfo=IST))
    assert (pools.get("userpool").count, api_replicas(kube)) == (0, 0)
    at(settings, scaler, datetime(2026, 10, 21, 7, 0, tzinfo=IST))
    assert (pools.get("userpool").count, api_replicas(kube)) == (3, 2)
    assert pools.get("systempool").count == 4


def test_schedule_acts_once_per_window(setup):
    settings, kube, pools, scaler = setup
    settings.nightly_enabled = True
    at(settings, scaler, datetime(2026, 10, 20, 21, 0, tzinfo=IST))
    settings.fixed_now = datetime(2026, 10, 20, 22, 0, tzinfo=IST)
    scaler.wake_namespace("dev1", "night-owl")
    at(settings, scaler, datetime(2026, 10, 20, 22, 5, tzinfo=IST))  # later run must not undo the wake
    assert api_replicas(kube) == 2
    at(settings, scaler, datetime(2026, 10, 21, 7, 0, tzinfo=IST))
    scheduled_wakes = lambda: [a for a in scaler.state.recent_audit(1000) if a["action"] == "wake" and a["actor"] == "schedule"]
    assert len(scheduled_wakes()) == 2  # dev1 + monitoring
    at(settings, scaler, datetime(2026, 10, 21, 7, 5, tzinfo=IST))
    assert len(scheduled_wakes()) == 2  # 07:05 run doesn't repeat it


def test_schedule_nightly_disabled_only_weekend(setup):
    settings, kube, pools, scaler = setup
    for when in (datetime(2026, 10, 20, 21, 0, tzinfo=IST), datetime(2026, 10, 21, 7, 0, tzinfo=IST)):
        at(settings, scaler, when)
        assert api_replicas(kube) == 2
    at(settings, scaler, datetime(2026, 10, 23, 21, 0, tzinfo=IST))
    assert api_replicas(kube) == 0
    for when in (datetime(2026, 10, 24, 7, 0, tzinfo=IST), datetime(2026, 10, 25, 7, 0, tzinfo=IST)):
        at(settings, scaler, when)
        assert pools.get("userpool").count == 0
    at(settings, scaler, datetime(2026, 10, 26, 7, 25, tzinfo=IST))  # missed runs: catches up later
    assert (pools.get("userpool").count, api_replicas(kube)) == (3, 2)


def test_schedule_different_weekend_and_night_times(setup):
    settings, kube, pools, scaler = setup
    settings.nightly_enabled = True
    settings.nightly_start_time, settings.nightly_end_time = time(23, 0), time(6, 0)
    settings.weekend_start_time, settings.weekend_end_time = time(18, 0), time(9, 0)
    at(settings, scaler, datetime(2026, 10, 22, 22, 0, tzinfo=IST))  # Thu 22:00: not yet
    assert api_replicas(kube) == 2
    at(settings, scaler, datetime(2026, 10, 22, 23, 0, tzinfo=IST))
    assert api_replicas(kube) == 0
    at(settings, scaler, datetime(2026, 10, 23, 6, 0, tzinfo=IST))   # Fri morning
    assert api_replicas(kube) == 2
    at(settings, scaler, datetime(2026, 10, 23, 18, 0, tzinfo=IST))  # Fri 18:00 weekend starts
    assert api_replicas(kube) == 0
    at(settings, scaler, datetime(2026, 10, 26, 6, 0, tzinfo=IST))   # Mon 06:00: weekend still on
    assert api_replicas(kube) == 0
    at(settings, scaler, datetime(2026, 10, 26, 9, 0, tzinfo=IST))
    assert api_replicas(kube) == 2


def test_schedule_does_not_wake_what_it_did_not_sleep(setup):
    settings, kube, pools, scaler = setup
    settings.nightly_enabled = True
    settings.fixed_now = datetime(2026, 10, 21, 12, 0, tzinfo=IST)
    scaler.sleep_namespace("dev1", "alice")  # slept by hand during the day
    at(settings, scaler, datetime(2026, 10, 21, 13, 0, tzinfo=IST))  # first ever run, after a night it didn't handle
    assert api_replicas(kube) == 0


def test_schedule_night_lease_expires(setup):
    settings, kube, pools, scaler = setup
    settings.nightly_enabled = True
    at(settings, scaler, datetime(2026, 10, 20, 21, 0, tzinfo=IST))
    settings.fixed_now = datetime(2026, 10, 20, 22, 0, tzinfo=IST)
    scaler.wake_namespace("dev1", "night-owl")
    assert pools.get("userpool").count == 3
    at(settings, scaler, datetime(2026, 10, 21, 6, 5, tzinfo=IST))  # lease (8h) ran out at 06:00
    assert (pools.get("userpool").count, api_replicas(kube)) == (0, 0)
    at(settings, scaler, datetime(2026, 10, 21, 7, 0, tzinfo=IST))
    assert (pools.get("userpool").count, api_replicas(kube)) == (3, 2)


# --- state persistence ----------------------------------------------------------

def test_state_survives_restart(setup):
    settings, _, _, scaler = setup
    scaler.state.save_pool_count("userpool", 3, overwrite=True)
    scaler.state.set_lease("dev1", "2026-10-24T18:00:00+05:30", "bob")
    scaler.state.set_schedule_mark("down", "2026-10-23T21:00:00+05:30")
    scaler.state.audit("bob", "wake", "dev1")
    reopened = State(settings)  # new process, same file
    assert reopened.pool_counts() == {"userpool": 3}
    assert reopened.leases() == {"dev1": {"until": "2026-10-24T18:00:00+05:30", "by": "bob"}}
    assert reopened.schedule_marks() == {"down": "2026-10-23T21:00:00+05:30"}
    assert reopened.recent_audit()[0]["actor"] == "bob"


def test_save_pool_count_overwrite(setup):
    _, _, _, scaler = setup
    scaler.state.save_pool_count("p", 3, overwrite=False)
    scaler.state.save_pool_count("p", 1, overwrite=False)
    assert scaler.state.pool_counts() == {"p": 3}
    scaler.state.save_pool_count("p", 5, overwrite=True)
    assert scaler.state.pool_counts() == {"p": 5}


def test_recent_audit_newest_first(setup):
    _, _, _, scaler = setup
    for i in range(5):
        scaler.state.audit("a", "scale", f"t{i}")
    assert [a["target"] for a in scaler.state.recent_audit(3)] == ["t4", "t3", "t2"]


# --- clusters without Flux (EKS) / with KEDA -----------------------------------

def test_flux_disabled_sleeps_without_kustomization(setup):
    settings, kube, _, scaler = setup
    settings.flux_enabled = False
    kube.flux.clear()
    scaler.sleep_namespace("dev1", "alice")  # no "Flux would revert" refusal
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 0
    scaler.wake_namespace("dev1", "alice")
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 2
    assert scaler.namespace_status("dev1").flux == []


def test_keda_paused_on_sleep_and_resumed_on_wake(setup):
    settings, kube, _, scaler = setup
    settings.keda_enabled = True
    kube.keda = {"job": KedaObject("ScaledJob", "dev1", "job", False, False),
                 "other": KedaObject("ScaledObject", "dev1", "other", True, False)}  # paused by someone else
    scaler.sleep_namespace("dev1", "alice")
    assert kube.keda["job"].paused and kube.keda["job"].paused_by_us
    scaler.wake_namespace("dev1", "alice")
    assert not kube.keda["job"].paused
    assert kube.keda["other"].paused  # not ours, left alone


def test_keda_untouched_when_disabled(setup):
    _, kube, _, scaler = setup
    kube.keda = {"job": KedaObject("ScaledJob", "dev1", "job", False, False)}
    scaler.sleep_namespace("dev1", "alice")
    assert not kube.keda["job"].paused


def test_eks_with_flux_and_keda(setup):
    settings, kube, _, scaler = setup
    settings.provider, settings.keda_enabled = "eks", True  # flux_enabled stays on
    kube.flux = {("Kustomization", n): FluxObject("Kustomization", "flux-system", n, False, False)
                 for n in ("apps-dev1", "dev1-reqs", "dev1-base")}  # several Kustomizations per namespace
    kube.flux_objects = lambda ns, flux_ns: [FluxObject(**vars(f)) for f in kube.flux.values()
                                             if f.name.removeprefix("apps-").split("-")[0] == ns]
    kube.keda = {"job": KedaObject("ScaledJob", "dev1", "job", False, False)}
    scaler.sleep_namespace("dev1", "alice")
    assert all(f.suspended_by_us for f in kube.flux.values())
    assert kube.keda["job"].paused_by_us
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 0
    scaler.wake_namespace("dev1", "alice")
    assert not any(f.suspended for f in kube.flux.values())
    assert not kube.keda["job"].paused
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 2


def test_eks_with_flux_still_requires_isolation(setup):
    settings, kube, _, scaler = setup
    settings.provider = "eks"
    del kube.flux[("Kustomization", "apps-dev1")]
    with pytest.raises(RuntimeError, match="Flux would revert"):
        scaler.sleep_namespace("dev1", "a")


def test_provider_requires_its_own_settings(monkeypatch):
    monkeypatch.setenv("SIESTA_PROVIDER", "eks")
    with pytest.raises(ValueError, match="SIESTA_AWS_REGION, SIESTA_EKS_CLUSTER_NAME"):
        Settings()
    monkeypatch.setenv("SIESTA_AWS_REGION", "us-east-1")
    monkeypatch.setenv("SIESTA_EKS_CLUSTER_NAME", "demo")
    assert Settings().cluster_name == "demo"


def test_namespace_owner_is_stored_shown_and_cleared(setup):
    _, _, _, scaler = setup
    assert scaler.namespace_status("dev1").owner == ""
    scaler.set_owner("dev1", "  Priya   Sharma ", "alice")
    assert scaler.namespace_status("dev1").owner == "Priya Sharma"
    assert scaler.state.recent_audit(1)[0]["action"] == "set-owner"
    scaler.set_owner("dev1", "", "alice")
    assert scaler.namespace_status("dev1").owner == ""


def test_owner_of_protected_namespace_refused(setup):
    _, _, _, scaler = setup
    with pytest.raises(PermissionError):
        scaler.set_owner("kube-system", "bob", "alice")


# --- Argo CD --------------------------------------------------------------------

AUTO = {"prune": True, "selfHeal": True}


@pytest.fixture
def argo(setup):
    settings, kube, pools, scaler = setup
    settings.flux_enabled, settings.argocd_enabled = False, True
    kube.flux.clear()
    kube.argocd = {
        "dev1": ("dev1", ArgoApp("argocd", "dev1", dict(AUTO), None)),
        "dev1-db": ("dev1", ArgoApp("team-a", "dev1-db", {}, None)),        # apps-in-any-namespace, default policy
        "dev1-manual": ("dev1", ArgoApp("argocd", "dev1-manual", None, None)),  # manual sync, not ours to touch
        "monitoring": ("monitoring", ArgoApp("argocd", "monitoring", dict(AUTO), None)),
    }
    return settings, kube, pools, scaler


def test_argocd_auto_sync_off_on_sleep_and_restored_on_wake(argo):
    _, kube, _, scaler = argo
    scaler.sleep_namespace("dev1", "alice")
    apps = {n: a for n, (_, a) in kube.argocd.items()}
    assert apps["dev1"].automated is None and apps["dev1-db"].automated is None
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 0
    assert scaler.namespace_status("dev1").state == "asleep"
    assert apps["monitoring"].automated == AUTO  # other namespace untouched
    scaler.wake_namespace("dev1", "alice")
    assert apps["dev1"].automated == AUTO and apps["dev1-db"].automated == {}
    assert apps["dev1-manual"].automated is None  # was manual before, stays manual
    assert ("pause", "dev1-manual") not in kube.argocd_calls and ("resume", "dev1-manual") not in kube.argocd_calls
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 2


def test_argocd_resumed_after_replicas_restored(argo):
    _, kube, _, scaler = argo
    scaler.sleep_namespace("dev1", "alice")
    replicas_when_resumed = []
    resume = kube.resume_argocd_app
    kube.resume_argocd_app = lambda app: (
        replicas_when_resumed.append(kube.workloads[("dev1", "Deployment", "api")].replicas), resume(app))
    scaler.wake_namespace("dev1", "alice")
    assert replicas_when_resumed and set(replicas_when_resumed) == {2}


def test_argocd_sleep_refused_without_labelled_application(argo):
    settings, kube, _, scaler = argo
    kube.argocd = {k: v for k, v in kube.argocd.items() if v[0] != "dev1"}
    with pytest.raises(RuntimeError, match="no Argo CD Application labelled kube-siesta.io/namespace=dev1"):
        scaler.sleep_namespace("dev1", "a")
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 2
    settings.require_argocd_isolation = False
    scaler.sleep_namespace("dev1", "a")
    assert kube.workloads[("dev1", "Deployment", "api")].replicas == 0


def test_argocd_reenabled_by_parent_keeps_first_saved_policy(argo):
    _, kube, _, scaler = argo
    scaler.sleep_namespace("dev1", "alice")
    app = kube.argocd["dev1"][1]
    app.automated = {"selfHeal": False}  # a parent app synced the child back while dev1 slept
    scaler.scale_workload("dev1", "Deployment", "api", 1, "alice")
    assert app.automated is None
    scaler.wake_namespace("dev1", "alice")
    assert app.automated == AUTO


def test_argocd_namespace_always_protected(argo):
    settings, _, _, scaler = argo
    assert scaler.is_protected("argocd")
    settings.argocd_enabled = False
    assert not scaler.is_protected("argocd")


def test_argocd_untouched_when_disabled(argo):
    settings, kube, _, scaler = argo
    settings.argocd_enabled = False
    scaler.sleep_namespace("dev1", "alice")
    assert kube.argocd_calls == []
    assert scaler.namespace_status("dev1").argocd == []
