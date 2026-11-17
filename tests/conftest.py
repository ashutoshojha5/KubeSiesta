import pytest

from app.config import Settings

# Settings have no defaults; tests use the same values as production
TEST_ENV = {
    "SIESTA_MANAGED_NAMESPACES": '["dev*", "staging", "ml-models", "monitoring"]',
    "SIESTA_PROTECTED_NAMESPACES": '["kube-*", "flux-system", "cert-manager", "vpn", "kube-siesta", "default"]',
    "SIESTA_AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
    "SIESTA_AZURE_RESOURCE_GROUP": "rg",
    "SIESTA_AKS_CLUSTER_NAME": "cluster",
    "SIESTA_PROTECTED_NODEPOOLS": "[]",
    "SIESTA_WEEKEND_NODE_COUNTS": '{"systempool": 1}',
    "SIESTA_DEFAULT_WAKE_NODE_COUNT": "1",
    "SIESTA_FLUX_NAMESPACE": "flux-system",
    "SIESTA_REQUIRE_FLUX_ISOLATION": "true",
    "SIESTA_NIGHTLY_ENABLED": "false",
    "SIESTA_WEEKEND_START_DAY": "fri",
    "SIESTA_WEEKEND_START_TIME": "21:00",
    "SIESTA_WEEKEND_END_DAY": "mon",
    "SIESTA_WEEKEND_END_TIME": "07:00",
    "SIESTA_NIGHTLY_START_TIME": "21:00",
    "SIESTA_NIGHTLY_END_TIME": "07:00",
    "SIESTA_TIMEZONE": "Asia/Kolkata",
    "SIESTA_LEASE_HOURS": "8",
    "SIESTA_STATE_DB_PATH": "",  # set per test to a temporary file
    "SIESTA_SCHEDULE_INTERVAL_SECONDS": "300",
}


@pytest.fixture(autouse=True)
def settings_env(monkeypatch, tmp_path):
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SIESTA_STATE_DB_PATH", str(tmp_path / "state.db"))
    # Ignore a developer's local .env
    monkeypatch.setitem(Settings.model_config, "env_file", None)
