# UI and API

## Who you are

There is no login. On the first visit the UI asks for your name, keeps it in a cookie, and writes it (with your IP address) to the audit log with everything you do. **Change name** clears it.

Because anyone who reaches the UI can act, keep it behind your VPN or VPC.

## Node pools

One row per node pool:

| Column | Meaning |
|---|---|
| Pool, Mode | Pool name, and System or User |
| VM size / Instance type | Node size |
| Nodes | Current node count |
| State | Provisioning state; shows progress while a pool is scaling |
| Weekend | Off-hours size, or *not scaled* |
| Saved count | Working-hours size saved for when off hours end |
| Action | Scale form, or *protected* / *autoscaler* when the pool can't be scaled by hand |

You rarely need this table: waking a namespace brings the user pools back by itself. Scaling a system pool down asks for confirmation, since it drains nodes running cluster add-ons. The UI allows 0 to 10 nodes per pool, and no fewer than 1 for system pools.

## Namespaces

Lists the managed namespaces and any namespace with an active lease. **Show all namespaces** adds the rest (protected namespaces are never shown).

| Column / button | What it does |
|---|---|
| Name | Click to see and scale single Deployments and StatefulSets. |
| Owner | Free text, e.g. a name or team, to show who to ask about the namespace. Saved and audited. |
| State | awake, asleep, partial or empty (see [How it works](how-it-works.md#sleeping-and-waking-a-namespace)). |
| Running | Running workloads / all workloads. |
| Flux, Argo CD, KEDA | Shown when enabled; see [Flux, Argo CD and KEDA](gitops-and-keda.md#what-the-ui-shows). |
| Weekend lease | When the lease ends, during off hours. |
| **Wake** | Brings node pools back if needed, restores every workload, resumes CronJobs, KEDA and GitOps. During off hours, starts a lease. |
| **Sleep** | Scales everything to 0 and remembers the counts. Asks for confirmation. |
| **Extend** | Resets the lease to the full `SIESTA_LEASE_HOURS`. |

**Scaling a single workload** (0 to 20 replicas) also pauses Flux, Argo CD and KEDA for the namespace, so they don't undo it, and remembers the original count. **Wake** later restores every workload to its original count and resumes everything. During off hours, scaling a workload up also starts a lease.

## Activity

Waking, sleeping and scaling run in the background, because node pools take minutes to scale. The Activity section shows the last 20 operations (running, done, or failed with the error) and the audit log of who did what.

## Configuration page

`/config` shows:

- the current time and whether you're in an off window, when it ends or when the next one starts,
- every setting with its live value, grouped, with an **i** button explaining each,
- a link to where settings are edited, if `SIESTA_CONFIG_URL` is set.

## HTTP API

| Method and path | Returns |
|---|---|
| `GET /healthz` | `200` when the app is up. Used by the probes. |
| `GET /api/config` | All settings as JSON. |
| `GET /api/nodepools` | Node pools: name, mode, VM size / instance type, node count, provisioning state, whether autoscaled or protected. |
| `GET /api/namespaces` | Managed namespaces: name, state, running and total workloads, owner, Flux/Argo CD isolation, KEDA paused, lease end. |

The actions (`POST /namespaces/<ns>/wake`, `/sleep`, `/extend`, `/owner`, `POST /namespaces/<ns>/workloads/<kind>/<name>/scale`, `POST /nodepools/<name>/scale`) are made for the UI. They need the name cookie (`siesta_user`), take form fields, and return HTML fragments. Protected namespaces and pools are refused with `403`.
