# Reconcile and reclamation

Four superadmin levers for finding out what the sandbox fleet and its deployment rows actually
look like, versus what the platform's own records say — and the one lever, among them, that
deletes anything. Reach for these to find out what is being billed for that nothing is tracking,
to stamp identity and age tags onto a fleet that predates them, to run the platform's routine
abandoned-sandbox sweep on demand instead of waiting for its own schedule, or to unstick
deployment rows a crashed pipeline left dangling.

Every lever below requires an authenticated superadmin session and is audited: the platform
records who ran it, when, and — for the levers that write or delete something — what changed.
None of them runs on its own schedule; run them on demand, when one of the situations below
applies.

## The four levers

| Lever | Reads | Writes | Deletes | Use it when |
|---|---|---|---|---|
| `POST /v1/admin/apps/reconcile-sandboxes` | Azure + Redis | — | never | you want the orphan inventory — containers Azure is billing for that no registry record tracks |
| `POST /v1/admin/apps/backfill-sandbox-tags` | Azure + the database | Azure resource tags | never | a fleet predates identity stamping: an untagged container carries no owner or age the sweep can read off ARM when Redis cannot be trusted |
| `POST /v1/build-sessions/internal/reap` | Redis + Azure | Redis | **yes** | you want to run the platform's routine abandoned-sandbox sweep right now, across the whole fleet, instead of waiting for its next scheduled pass |
| `POST /v1/admin/apps/reconcile-deploys` | the database + Azure | the database | never | a deployment row is stuck because the process driving it died mid-pipeline |

## Which lever answers which question

- **What is leaking?** `reconcile-sandboxes`. Its "unregistered" count is the leak: containers
  Azure is billing for that no registry record tracks, so the scheduled sweep — which only ever
  reaches what the registry still names — will never reach them either.
- **Why does a container look like it has no age?** `backfill-sandbox-tags`. It stamps identity
  and a creation time onto any container that predates that stamping, which is what lets the
  sweep's own age ceiling read a real age off ARM instead of the registry record — the registry's
  own birthday is re-stamped at every registration and would hand the wrong containers a fresh
  clock. Read its "skipped, no matching application row" count carefully: those containers were
  stamped with only their kind, because no application record matched their name, and stay
  unowned until a human deletes them by hand (see below).
- **Is the scheduled worker alive?** None of these levers answers that. The sweep logs a line on
  every pass and the conversation-retention pass records a row every day; the
  [Taskiq worker runbook](taskiq-worker.md) covers reading both, and why neither proves the
  worker process as a whole is alive.

## Blast radius

Only two things ever delete a container: the reap lever above, and the scheduled sweep it runs on
demand — the same code path either way. Everything else only reports. That is a deliberate
design, not an unfinished feature: a container provisioned seconds ago, before the build that owns
it has finished writing its own registry record, looks exactly like an orphan for a short window,
and that ambiguity is not something to hand an irreversible delete.

The reap lever, on its scheduled cadence, additionally refuses to run anywhere except the
production control plane, regardless of how its flags are set.

Every lever here requires an authenticated superadmin session. Treat that session the way you
would any credential with this much reach — do not leave one open on a shared machine.

## Deleting by hand

The platform will not do this for you, deliberately. A container `reconcile-sandboxes` names as
unregistered, or that `backfill-sandbox-tags` leaves unowned, is not touched by any automatic
pass:

1. Confirm the name is not one a live builder is using — check the response of
   `reconcile-sandboxes`, not your memory.
2. Delete the Container Apps instance directly, by name — for example:
   `az containerapp delete -n <container name> -g <resource group> --yes`.
3. Nothing else is required. The registry record, if any, is cleared by the next sweep.
