"""FastAPI app: HTMX dashboard plus a small JSON API.

There is no login (the app is only reachable over the VPN). Users type their name once; it is kept
in a cookie and written to the audit log with every action.
"""

import html
import json
import logging
import threading
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.config_help import HELP
from app.deps import get_scaler
from app.scaler import Scaler
from app.scheduler import Scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)



@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    scheduler = Scheduler(get_scaler(), get_settings().schedule_interval_seconds)
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="KubeSiesta", lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

USER_COOKIE = "siesta_user"
WORKLOAD_KINDS = {"Deployment", "StatefulSet"}

# Long-running actions (node pool scaling takes minutes) run in the background; this keeps
# the most recent ones so the page can show progress. Single replica, so memory is fine.
_operations: OrderedDict[str, dict] = OrderedDict()
_ops_lock = threading.Lock()


def _run_operation(description: str, fn: Callable[[], None]) -> None:
    op_id = uuid.uuid4().hex[:8]
    with _ops_lock:
        _operations[op_id] = {"description": description, "status": "running", "error": ""}
        while len(_operations) > 20:
            _operations.popitem(last=False)
    try:
        fn()
        _operations[op_id]["status"] = "done"
    except Exception as e:
        log.exception("operation failed: %s", description)
        _operations[op_id].update(status="failed", error=str(e))


def current_user(request: Request) -> str:
    return " ".join(request.cookies.get(USER_COOKIE, "").split())


def actor(request: Request) -> str:
    name = current_user(request)
    if not name:
        raise HTTPException(400, "Set your name first")
    client = request.headers.get("x-forwarded-for", request.client.host if request.client else "?")
    return f"{name} ({client.split(',')[0].strip()})"


Actor = Annotated[str, Depends(actor)]
ScalerDep = Annotated[Scaler, Depends(get_scaler)]


def _queue(background: BackgroundTasks, description: str, fn: Callable[[], None]) -> HTMLResponse:
    background.add_task(_run_operation, description, fn)
    return HTMLResponse(f'<span class="queued">Queued: {html.escape(description)}</span>',
                        headers={"HX-Trigger": "refresh"})


# --- pages ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    settings = get_settings()
    user = current_user(request)
    if not user:
        return templates.TemplateResponse(request, "name.html", {"settings": settings, "name": "", "error": ""})
    return templates.TemplateResponse(request, "index.html", {
        "user": user,
        "off_window_ends_at": settings.off_window_ends_at(),
        "settings": settings,
    })


@app.post("/whoami")
def whoami(request: Request, name: Annotated[str, Form()] = ""):
    name = " ".join(name.split())[:64]  # trim and collapse whitespace
    if not name:
        return templates.TemplateResponse(request, "name.html", {
            "settings": get_settings(), "name": "", "error": "Please enter your name.",
        }, status_code=400)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(USER_COOKIE, name, max_age=180 * 24 * 3600, httponly=True, samesite="strict")
    return response


@app.post("/forget-name")
def forget_name():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(USER_COOKIE, httponly=True, samesite="strict")
    return response


@app.get("/partials/nodepools", response_class=HTMLResponse)
def nodepools_partial(request: Request, scaler: ScalerDep):
    saved = scaler.state.pool_counts()
    pools = scaler.pools.list()
    weekend = {p.name: scaler.weekend_count(p) for p in pools}
    return templates.TemplateResponse(request, "partials/nodepools.html",
                                      {"pools": pools, "saved": saved, "weekend": weekend,
                                       "settings": get_settings()})


@app.get("/partials/namespaces", response_class=HTMLResponse)
def namespaces_partial(request: Request, scaler: ScalerDep, show_all: bool = False):
    leases = scaler.state.active_leases()
    names = [ns for ns in scaler.kube.list_namespaces() if not scaler.is_protected(ns)]
    if not show_all:
        names = [ns for ns in names if scaler.is_managed(ns) or ns in leases]
    owners = scaler.state.owners()
    statuses = [scaler.namespace_status(ns, leases, owners) for ns in names]
    return templates.TemplateResponse(request, "partials/namespaces.html",
                                      {"namespaces": statuses, "show_all": show_all,
                                       "settings": get_settings()})


@app.get("/partials/namespaces/{ns}", response_class=HTMLResponse)
def workloads_partial(request: Request, ns: str, scaler: ScalerDep):
    return templates.TemplateResponse(request, "partials/workloads.html",
                                      {"ns": scaler.namespace_status(ns), "settings": get_settings()})


@app.get("/partials/activity", response_class=HTMLResponse)
def activity_partial(request: Request, scaler: ScalerDep):
    return templates.TemplateResponse(request, "partials/activity.html", {
        "operations": list(reversed(_operations.values())),
        "audit": scaler.state.recent_audit(50),
    })


# --- configuration ----------------------------------------------------------

def _config_sections(settings) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """Current settings grouped for display, as (SIESTA_ env var, value, help) rows. Only the chosen
    provider's settings are shown."""
    values = settings.model_dump(mode="json")
    cloud = (["azure_subscription_id", "azure_resource_group", "aks_cluster_name"] if settings.provider == "aks"
             else ["aws_region", "eks_cluster_name", "system_nodepools", "autoscaled_nodepools"])
    groups = [
        ("Cluster", ["provider", *cloud, "protected_nodepools", "weekend_node_counts", "default_wake_node_count"]),
        ("Namespaces", ["managed_namespaces", "protected_namespaces"]),
        ("GitOps / autoscaling", ["flux_enabled", "flux_namespace", "require_flux_isolation", "argocd_enabled",
                                  "argocd_namespace", "require_argocd_isolation", "keda_enabled"]),
        ("Schedule", ["timezone", "weekend_start_day", "weekend_start_time", "weekend_end_day", "weekend_end_time",
                      "nightly_enabled", "nightly_start_time", "nightly_end_time", "lease_hours",
                      "schedule_interval_seconds"]),
        ("State", ["state_db_path", "label_prefix", "config_url"]),
    ]
    return [(title, [(f"SIESTA_{k.upper()}",
                      json.dumps(values[k]) if isinstance(values[k], (list, dict)) else str(values[k]),
                      HELP[k]) for k in keys]) for title, keys in groups]


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    settings = get_settings()
    now = settings.now()
    window = settings.current_off_window(now)
    upcoming = [w for w in settings.off_windows(now) if w[0] > now]
    return templates.TemplateResponse(request, "config.html", {
        "settings": settings, "now": now, "window": window,
        "next_window": upcoming[0] if upcoming else None,
        "sections": _config_sections(settings),
    })


@app.get("/api/config")
def api_config():
    return get_settings().model_dump(mode="json")


# --- actions ----------------------------------------------------------------

def _modifiable(scaler: Scaler, ns: str) -> None:
    if scaler.is_protected(ns):
        raise HTTPException(403, f"{ns} is protected")


@app.post("/namespaces/{ns}/sleep", response_class=HTMLResponse)
def sleep_ns(ns: str, who: Actor, scaler: ScalerDep, background: BackgroundTasks):
    _modifiable(scaler, ns)
    return _queue(background, f"sleep {ns}", lambda: scaler.sleep_namespace(ns, who))


@app.post("/namespaces/{ns}/wake", response_class=HTMLResponse)
def wake_ns(ns: str, who: Actor, scaler: ScalerDep, background: BackgroundTasks):
    _modifiable(scaler, ns)
    return _queue(background, f"wake {ns}", lambda: scaler.wake_namespace(ns, who))


@app.post("/namespaces/{ns}/owner", response_class=HTMLResponse)
def set_owner(ns: str, who: Actor, scaler: ScalerDep, owner: Annotated[str, Form()] = ""):
    _modifiable(scaler, ns)
    scaler.set_owner(ns, owner, who)
    return HTMLResponse('<span class="queued">Owner saved</span>', headers={"HX-Trigger": "refresh"})


@app.post("/namespaces/{ns}/extend", response_class=HTMLResponse)
def extend_ns(ns: str, who: Actor, scaler: ScalerDep, background: BackgroundTasks):
    _modifiable(scaler, ns)
    return _queue(background, f"extend {ns}", lambda: scaler.extend_lease(ns, who))


@app.post("/namespaces/{ns}/workloads/{kind}/{name}/scale", response_class=HTMLResponse)
def scale_workload(ns: str, kind: str, name: str, replicas: Annotated[int, Form(ge=0, le=20)],
                   who: Actor, scaler: ScalerDep, background: BackgroundTasks):
    _modifiable(scaler, ns)
    if kind not in WORKLOAD_KINDS:
        raise HTTPException(400, f"unsupported kind {kind}")
    return _queue(background, f"scale {kind} {ns}/{name} to {replicas}",
                  lambda: scaler.scale_workload(ns, kind, name, replicas, who))


@app.post("/nodepools/{name}/scale", response_class=HTMLResponse)
def scale_pool(name: str, count: Annotated[int, Form(ge=0, le=10)],
               who: Actor, scaler: ScalerDep, background: BackgroundTasks):
    if scaler.pools.get(name).protected:
        raise HTTPException(403, f"node pool {name} is protected")
    return _queue(background, f"scale node pool {name} to {count}",
                  lambda: scaler.scale_pool(name, count, who))


# --- JSON API & health ------------------------------------------------------

@app.get("/api/nodepools")
def api_nodepools(scaler: ScalerDep):
    return scaler.pools.list()


@app.get("/api/namespaces")
def api_namespaces(scaler: ScalerDep):
    leases = scaler.state.active_leases()
    return [
        {"name": s.name, "state": s.state, "managed": s.managed, "running": s.running,
         "workloads": len(s.workloads), "owner": s.owner, "flux_isolated": s.flux_isolated, "argocd_isolated": s.argocd_isolated, "keda_paused": any(k.paused for k in s.keda), "lease_until": s.lease_until}
        for s in (scaler.namespace_status(ns, leases) for ns in scaler.managed_namespaces())
    ]


@app.get("/healthz")
def healthz():
    return {"ok": True}
