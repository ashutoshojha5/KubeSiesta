# Development

## Run locally

Requires Python 3.15 and [uv](https://docs.astral.sh/uv/).

```sh
uv venv && uv pip install -e '.[dev]'
cp .env.example .env
.venv/bin/uvicorn app.main:app --reload
```

Open http://localhost:8000.

`.env.example` has the same sections as the chart's values: keep **one** cloud section (AKS or EKS) and **one** deploy section (plain manifests, Flux or Argo CD) uncommented, set KEDA, and replace the example values.

> The app uses your current kube context and your cloud credentials (`az login`, or `AWS_PROFILE`). **Changes are real**, and the scheduler runs locally too: if you're inside an off window, it will start scaling the cluster down. Point it at a test cluster, or set the windows so you're outside them.

## Tests

```sh
.venv/bin/pytest
```

Tests use fake Kubernetes and cloud clients and a fixed clock; they don't need a cluster. The test settings are in `tests/conftest.py`.

Check the chart after changing it:

```sh
helm lint deploy/helm/kube-siesta
helm template t deploy/helm/kube-siesta --set provider=eks --set argocd.enabled=true
```

## Code layout

```
app/
  config.py       settings (SIESTA_*), validation and the off-window calculation
  config_help.py  the explanation of each setting shown on /config (a test checks every setting has one)
  kube.py         Kubernetes, Flux, Argo CD and KEDA API calls
  nodepools.py    NodePool model and the provider switch
  aks.py          AKS node pools (VMSS and VirtualMachines pools)
  eks.py          EKS node groups (Auto Scaling Groups, managed nodegroups)
  scaler.py       sleep/wake orchestration, off-hours schedule, lease expiry
  state.py        SQLite state (node counts, leases, owners, schedule marks, audit)
  scheduler.py    background thread running the schedule every SIESTA_SCHEDULE_INTERVAL_SECONDS
  deps.py         wiring of the objects above
  main.py         FastAPI app: pages, HTMX partials, actions, JSON API
  templates/      Jinja2 templates
deploy/
  helm/kube-siesta/          Helm chart
  setup-azure-identity.sh    one-time AKS identity setup
  setup-aws-irsa.sh          one-time EKS IAM role setup
tests/
Dockerfile
.github/workflows/docker.yml   builds the amd64/arm64 image and pushes it to Docker Hub
```

## Adding a setting

1. Add the field to `Settings` in `app/config.py`, with a comment.
2. Add its explanation to `app/config_help.py`.
3. Add it to a group in `_config_sections` in `app/main.py` so `/config` shows it.
4. Add it to `.env.example` and, if it should have a chart default, under `config` in `values.yaml`.
5. Document it in [Configuration](configuration.md).

## Releasing

`.github/workflows/docker.yml` builds the image on every push and pull request, and pushes it to Docker Hub ([`ashutoshojha5/kubesiesta`](https://hub.docker.com/r/ashutoshojha5/kubesiesta)) for pushes to `main` and for version tags. It needs the repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`.

To release a version, bump `version` in `pyproject.toml` and `version`/`appVersion` in the chart's `Chart.yaml`, then tag it:

```sh
git tag v0.1.0 && git push origin v0.1.0   # publishes image tags 0.1.0 and 0.1
```
