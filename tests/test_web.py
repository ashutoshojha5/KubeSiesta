import pytest
from fastapi.testclient import TestClient

from app.main import USER_COOKIE, app


@pytest.fixture
def client():
    # Not used as a context manager, so the scheduler (lifespan) doesn't start
    return TestClient(app, base_url="https://testserver")


def test_first_visit_asks_for_name(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "What's your name?" in r.text
    assert 'action="/whoami"' in r.text
    assert "Node pools" not in r.text


def test_whitespace_cookie_counts_as_no_name(client):
    client.cookies.set(USER_COOKIE, "   ")
    assert "What's your name?" in client.get("/").text


def test_submitting_name_sets_cookie_and_shows_dashboard(client):
    r = client.post("/whoami", data={"name": "  Priya   Sharma "}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f'{USER_COOKIE}="Priya Sharma"') or cookie.startswith(f"{USER_COOKIE}=Priya")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    page = client.get("/").text
    assert "Signed in as" in page and "Priya Sharma" in page and "Node pools" in page


@pytest.mark.parametrize("name", ["", "   "])
def test_empty_name_rejected(client, name):
    r = client.post("/whoami", data={"name": name}, follow_redirects=False)
    assert r.status_code == 400
    assert "Please enter your name." in r.text
    assert "set-cookie" not in r.headers


def test_name_is_escaped(client):
    client.post("/whoami", data={"name": "<script>x</script>"})
    page = client.get("/").text
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page


def test_change_name_clears_cookie(client):
    client.post("/whoami", data={"name": "Priya"})
    r = client.post("/forget-name", follow_redirects=False)
    assert r.status_code == 303
    assert USER_COOKIE not in client.cookies
    assert "What's your name?" in client.get("/").text


def test_actions_require_name(client):
    r = client.post("/namespaces/dev1/sleep")
    assert r.status_code == 400 and "Set your name first" in r.text


def test_config_page_shows_current_settings(client):
    r = client.get("/config")
    assert r.status_code == 200
    assert "SIESTA_MANAGED_NAMESPACES" in r.text and "SIESTA_AKS_CLUSTER_NAME" in r.text
    assert "SIESTA_AWS_REGION" not in r.text  # other provider's settings are hidden
    assert "Off hours" in r.text


def test_config_link_on_dashboard_and_json(client):
    client.cookies.set(USER_COOKIE, "Priya")
    assert 'href="/config"' in client.get("/").text
    assert client.get("/api/config").json()["provider"] == "aks"


def test_every_setting_has_help():
    from app.config import Settings
    from app.config_help import HELP
    assert set(Settings.model_fields) <= set(HELP)


def test_config_page_has_info_button_and_help_per_setting(client):
    page = client.get("/config").text
    assert 'aria-label="About SIESTA_LEASE_HOURS"' in page
    assert "How long a namespace woken during off hours stays up" in page


def test_config_page_links_to_where_settings_are_edited(client, monkeypatch):
    from app.config import get_settings
    assert "To change any of these settings" not in client.get("/config").text  # not set in tests
    monkeypatch.setenv("SIESTA_CONFIG_URL", "https://example.test/configmap.yaml")
    get_settings.cache_clear()
    try:
        assert 'href="https://example.test/configmap.yaml"' in client.get("/config").text
    finally:
        get_settings.cache_clear()


def test_namespaces_show_argocd_column(client, monkeypatch):
    from app.config import get_settings
    from app.deps import get_scaler
    from app.kube import ArgoApp
    from app.scaler import Scaler
    from app.state import State
    from test_scaler import FakeKube, FakePools, wl

    monkeypatch.setenv("SIESTA_FLUX_ENABLED", "false")
    monkeypatch.setenv("SIESTA_ARGOCD_ENABLED", "true")
    get_settings.cache_clear()
    settings = get_settings()
    kube = FakeKube([wl("dev1", "api", 2), wl("dev2", "api", 1)])
    kube.argocd = {"dev1": ("dev1", ArgoApp("argocd", "dev1", {"selfHeal": True}, None))}
    app.dependency_overrides[get_scaler] = lambda: Scaler(kube, FakePools([]), State(settings), settings)
    try:
        page = client.get("/partials/namespaces").text
        assert "<th>Argo CD</th>" in page and "<th>Flux</th>" not in page
        assert page.count("not isolated") == 1  # dev2 has no labelled Application
        detail = client.get("/partials/namespaces/dev1").text
        assert "pauses Argo CD auto-sync" in detail
        assert client.get("/api/namespaces").json()[0]["argocd_isolated"] is True
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
