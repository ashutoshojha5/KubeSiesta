# How it works

KubeSiesta is one pod with a web UI and a scheduler thread. It changes three kinds of things: workload replica counts, node pool sizes, and (when enabled) Flux, Argo CD and KEDA objects so they don't undo its changes.

## Off hours

Off hours are made of two kinds of window, both in `SIESTA_TIMEZONE`:

- **Weekly window**, always on: from `SIESTA_WEEKEND_START_DAY` at `SIESTA_WEEKEND_START_TIME` to `SIESTA_WEEKEND_END_DAY` at `SIESTA_WEEKEND_END_TIME`. Example: Friday 21:00 to Monday 07:00.
- **Nightly window**, only when `SIESTA_NIGHTLY_ENABLED=true`: every day from `SIESTA_NIGHTLY_START_TIME` to `SIESTA_NIGHTLY_END_TIME`, ending the next day if the end is earlier than the start.

Windows that touch or overlap are merged. With both enabled, Friday night runs straight into the weekend, so the cluster isn't woken at 07:00 on Saturday.

The `/config` page shows the current and the next off window.

## The schedule

Every `SIESTA_SCHEDULE_INTERVAL_SECONDS` the scheduler checks where it is:

- **An off window has started** and this window hasn't been handled yet: scale everything down.
  1. Put every managed namespace to sleep.
  2. Shrink node pools to their off-hours size (see [Node pools](#node-pools)), user pools first, system pools last.
- **An off window has ended** and the app scaled it down: scale everything up.
  1. Bring node pools back to their saved size, system pools first.
  2. Wake every managed namespace.
- **Inside an off window**: expire leases (see [Leases](#leases)).

Each window is handled once, and the app records which windows it has handled in its state database. If the pod was down when a window started or ended, it catches up on its next run. A window it never scaled down (for example because the app was installed mid-weekend) is not scaled up, so namespaces someone put to sleep by hand stay asleep.

If any namespace fails to go to sleep, the node pools are **left up** on purpose (removing nodes under running workloads would break them) and the run is retried on the next interval. The error is in the pod log.

## Sleeping and waking a namespace

| Step | Sleep | Wake |
|---|---|---|
| 1 | Suspend Flux / turn off Argo CD auto-sync for the namespace *(if enabled)* | Bring user node pools at 0 back up |
| 2 | Save each Deployment's and StatefulSet's replicas in a `<prefix>/original-replicas` annotation, then scale it to 0 | Restore replicas from that annotation and remove it |
| 3 | Suspend CronJobs | Resume the CronJobs it suspended |
| 4 | Pause KEDA ScaledObjects/ScaledJobs *(if enabled)* | Resume the KEDA objects it paused |
| 5 | | Resume Flux / restore Argo CD auto-sync and ask them to sync now |

GitOps is paused first so it can't put replicas back while the app works, and resumed last so the sync it triggers starts from the restored state.

Everything the app pauses is marked with a `<prefix>/...` annotation, and it only resumes what it marked. A CronJob, Flux object, Application or KEDA object that someone else had already paused stays paused after a wake.

A namespace's **state** in the UI comes from these markers:

| State | Meaning |
|---|---|
| awake | Workloads running, nothing paused by the app |
| asleep | Nothing running; the app has saved replica counts |
| partial | Some workloads running and some saved, for example after scaling one workload by hand |
| empty | No Deployments or StatefulSets |

## Leases

During off hours anyone with access to the UI can wake a namespace. Waking it:

1. brings user node pools at 0 back to their saved size (or `SIESTA_DEFAULT_WAKE_NODE_COUNT` if none was saved),
2. wakes the namespace,
3. gives it a **lease** of `SIESTA_LEASE_HOURS`. **Extend** in the UI resets the lease to the full length.

On each scheduler run during off hours:

- namespaces whose lease has run out go back to sleep;
- managed namespaces that are running without a lease (for example someone scaled a Deployment with `kubectl`) go back to sleep;
- once no namespace has an active lease, the node pools go back down.

Outside off hours there are no leases: a namespace you wake stays awake.

## Node pools

For each pool the off-hours size is:

| Pool | Off-hours size |
|---|---|
| Protected (`SIESTA_PROTECTED_NODEPOOLS`) | not scaled |
| Managed by the cluster autoscaler | not scaled |
| Listed in `SIESTA_WEEKEND_NODE_COUNTS` | the listed count |
| Other user pool | 0 |
| Other system pool | not scaled |

System pools never go below 1 node. Before shrinking a pool the app saves its working-hours size and uses it when off hours end. During off hours, scaling a pool by hand in the UI does not change that saved size, so a developer scaling up and down on a Saturday doesn't change what Monday restores.

The pod itself should run on a pool that stays up (a system pool); otherwise it scales away its own node. The Helm chart does this by default; see [Helm values](helm-values.md#cloud).

## State

State that must survive a restart is kept in a SQLite file (`SIESTA_STATE_DB_PATH`) on a ReadWriteOnce volume:

- saved node pool sizes,
- leases,
- namespace owners,
- which off windows were scaled down and up,
- the audit log.

Saved replica counts are **not** in the database; they live on the workloads as annotations, so the cluster itself shows what is asleep.

Because the file can only have one writer, the app always runs as a single replica.
