import re
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIESTA_", env_file=".env")

    # Every setting comes from a SIESTA_* environment variable (the kube-siesta-config ConfigMap in
    # the cluster, or .env locally). Settings without a default are required, so a missing one fails at startup.
    # Lists and dicts are JSON, times are HH:MM.

    # Namespaces put to sleep on weekends (fnmatch patterns)
    managed_namespaces: list[str]
    # Namespaces the UI will never modify
    protected_namespaces: list[str]

    # Which cloud the node pools live in. Only the settings for the chosen provider are required.
    provider: Literal["aks", "eks"] = "aks"

    # AKS
    azure_subscription_id: str | None = None
    azure_resource_group: str | None = None
    aks_cluster_name: str | None = None
    # EKS: node groups are the Auto Scaling Groups tagged kubernetes.io/cluster/<eks_cluster_name>
    aws_region: str | None = None
    eks_cluster_name: str | None = None
    # EKS has no system/user node pool modes; pools named here are treated as System (never below 1 node)
    system_nodepools: list[str] = []
    # EKS managed nodegroups left to the cluster autoscaler (self-managed ones are detected by min != max)
    autoscaled_nodepools: list[str] = []

    # Node pools never scaled
    protected_nodepools: list[str]
    # Node count per pool during the off window. User pools not listed go to 0;
    # System pools not listed are left alone (AKS needs at least 1 node in them).
    weekend_node_counts: dict[str, int]
    # Count used when waking a pool that has no saved count
    default_wake_node_count: int

    # Flux. Set flux_enabled=false for clusters that don't use it (e.g. Argo CD, or manifests applied with kubectl).
    flux_enabled: bool = True
    flux_namespace: str = "flux-system"
    # Refuse to sleep a namespace unless a Flux Kustomization is labelled for it; otherwise a
    # shared Kustomization would put the replicas back on its next reconcile
    require_flux_isolation: bool = True
    # Argo CD. While a namespace sleeps, its Applications (labelled <label_prefix>/namespace=<ns>, in any
    # namespace) have automated sync turned off so Argo doesn't sync the replicas back.
    argocd_enabled: bool = False
    argocd_namespace: str = "argocd"
    # Refuse to sleep a namespace unless an Application is labelled for it
    require_argocd_isolation: bool = True
    # Pause KEDA ScaledObjects/ScaledJobs while a namespace sleeps, so they don't scale it back up
    keda_enabled: bool = False

    # Prefix of the labels and annotations this app reads and writes (<prefix>/original-replicas,
    # <prefix>/suspended, <prefix>/namespace). Changing it on a cluster with sleeping namespaces
    # loses track of them, so wake everything first.
    label_prefix: str = "kube-siesta.io"

    # Off hours = the weekend window, plus every night when nightly_enabled. Overlapping
    # windows merge (e.g. Friday night runs straight into the weekend).
    timezone: str
    # Weekend: weekend_start_day weekend_start_time -> weekend_end_day weekend_end_time
    weekend_start_day: str
    weekend_start_time: time
    weekend_end_day: str
    weekend_end_time: time
    # Nightly: every day nightly_start_time -> nightly_end_time (next day if it wraps midnight)
    nightly_enabled: bool
    nightly_start_time: time
    nightly_end_time: time
    # How long a namespace woken during the off window stays up
    lease_hours: int

    # Where these settings are edited (the ConfigMap in git); shown as a link on the /config page
    config_url: str | None = None

    # SQLite file for saved node counts, leases, schedule progress and the audit log
    # (on the kube-siesta PersistentVolume in the cluster)
    state_db_path: str
    # How often the in-process scheduler checks the off windows and leases
    schedule_interval_seconds: int

    @model_validator(mode="after")
    def _provider_settings(self) -> "Settings":
        required = {
            "aks": ["azure_subscription_id", "azure_resource_group", "aks_cluster_name"],
            "eks": ["aws_region", "eks_cluster_name"],
        }[self.provider]
        missing = [f"SIESTA_{f.upper()}" for f in required if not getattr(self, f)]
        if missing:
            raise ValueError(f"provider {self.provider} requires {', '.join(missing)}")
        return self

    @field_validator("weekend_start_day", "weekend_end_day")
    @classmethod
    def _weekday(cls, v: str) -> str:
        v = v.lower()
        if v not in WEEKDAYS:
            raise ValueError(f"must be one of {WEEKDAYS}")
        return v

    @field_validator("label_prefix")
    @classmethod
    def _label_prefix(cls, v: str) -> str:
        # Kubernetes label/annotation prefixes must be DNS subdomains
        if len(v) > 253 or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*", v):
            raise ValueError(f"{v!r} is not a DNS subdomain (e.g. kube-siesta.example.com)")
        return v

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v

    @property
    def cluster_name(self) -> str:
        return (self.eks_cluster_name if self.provider == "eks" else self.aks_cluster_name) or ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def off_windows(self, around: datetime) -> list[tuple[datetime, datetime]]:
        """Merged off-hours windows from about a week before `around` to a week after."""
        around = around.astimezone(self.tz)
        windows = []
        for offset in range(-8, 9):
            day = (around + timedelta(days=offset)).date()
            if self.nightly_enabled:
                windows.append(self._window(day, self.nightly_start_time, 0, self.nightly_end_time, wrap_days=1))
            if day.weekday() == WEEKDAYS.index(self.weekend_start_day):
                days = (WEEKDAYS.index(self.weekend_end_day) - day.weekday()) % 7
                windows.append(self._window(day, self.weekend_start_time, days, self.weekend_end_time, wrap_days=7))
        merged: list[tuple[datetime, datetime]] = []
        for start, end in sorted(windows):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def _window(self, day: date, start: time, days: int, end: time, wrap_days: int) -> tuple[datetime, datetime]:
        """Window from `day` at `start` to `days` later at `end`. If that end isn't after the
        start, the window wraps by `wrap_days` (1 for 21:00 -> 07:00, 7 for a full week)."""
        begin = datetime.combine(day, start, self.tz)
        finish = datetime.combine(day + timedelta(days=days), end, self.tz)
        if finish <= begin:
            finish += timedelta(days=wrap_days)
        return begin, finish

    def current_off_window(self, when: datetime | None = None) -> tuple[datetime, datetime] | None:
        when = when or self.now()
        return next(((s, e) for s, e in self.off_windows(when) if s <= when < e), None)

    def previous_off_window(self, when: datetime | None = None) -> tuple[datetime, datetime] | None:
        when = when or self.now()
        return next(((s, e) for s, e in reversed(self.off_windows(when)) if e <= when), None)

    def in_off_window(self, when: datetime | None = None) -> bool:
        return self.current_off_window(when) is not None

    def off_window_ends_at(self, when: datetime | None = None) -> datetime | None:
        """When the current off window ends, or None if we're not in one."""
        window = self.current_off_window(when)
        return window[1] if window else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
