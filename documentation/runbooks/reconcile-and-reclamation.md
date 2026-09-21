# Reconcile and reclamation

Five superadmin levers for finding out what the sandbox fleet and its deployment rows actually
look like, versus what the platform's own records say — and the one lever, among them, that
deletes anything. Reach for these to find out what is being billed for that nothing is tracking,
to preview what the automatic reclamation pass would remove before it is allowed to delete
anything, to recover an untagged fleet before that pass is allowed to run destructively, to run
the platform's routine abandoned-sandbox sweep on demand instead of waiting for its own schedule,
or to unstick deployment rows a crashed pipeline left dangling.

Every lever below requires an authenticated superadmin session and is audited: the platform
records who ran it, when, and — for the levers that write or delete something — what changed.
None of them runs on its own schedule; run them on demand, when one of the situations below
applies.

## The five levers

| Lever | Reads | Writes | Deletes | Use it when |
|---|---|---|---|---|
| `POST /v1/admin/apps/reclamation-report` | Azure + Redis + the database | — | never | you want to know what the automatic reclamation pass would do right now — before turning on the flag that lets it delete anything, or while triaging an escalation |
| `POST /v1/admin/apps/reconcile-sandboxes` | Azure + Redis | — | never | you want the orphan inventory — containers Azure is billing for that no registry record tracks — or you want to check whether the reclamation pass is still running at all |
| `POST /v1/admin/apps/backfill-sandbox-tags` | Azure + the database | Azure resource tags | never | before the destroy flag is armed: a container with no identity tags cannot be judged and stays in escalation forever until it is tagged |
| `POST /v1/build-sessions/internal/reap` | Redis + Azure | Redis | **yes** | you want to run the platform's routine abandoned-sandbox sweep right now, across the whole fleet, instead of waiting for its next scheduled pass |
| `POST /v1/admin/apps/reconcile-deploys` | the database + Azure | the database | never | a deployment row is stuck because the process driving it died mid-pipeline |

## Which lever answers which question

- **Is the scheduled reclamation worker alive?** `reconcile-sandboxes`, reading its "last
  reclamation pass" and "stale" fields. A stale or missing pass timestamp is the only signal that
  distinguishes a dead worker from a genuinely quiet fleet — every other alarm is raised by the
  pass itself, so a worker that has stopped running raises none of them.
- **What would reclamation do?** `reclamation-report`. It runs the same classifier the scheduled
  pass uses, in the request, and can neither stage nor destroy anything — those two actions live
  only inside the scheduled worker task and are not reachable through this endpoint. It answers
  the question just as readily with the reclamation flags off as with them on, which is the point:
  the deployment deciding whether to turn reclamation on is exactly the one that needs to see the
  preview first.
- **What is leaking?** `reconcile-sandboxes`. Its "unregistered" count is the leak: containers
  Azure is billing for that no registry record tracks, so no other sweep will ever reach them.
- **Why is a container stuck in escalation?** `backfill-sandbox-tags`. Read its "skipped, no
  matching application row" count carefully — those containers were stamped with only their kind,
  because no application record matched their name, and they stay in escalation until a human
  deletes them by hand (see `reclamation-escalation.md`).

## Blast radius

Only two things ever delete a container: the reap lever above, and the scheduled reclamation
pass's own destroy step, which is not reachable through any of the other four endpoints in this
list. Everything else only reports. That is a deliberate design, not an unfinished feature: a
container provisioned seconds ago, before the build that owns it has finished writing its own
registry record, looks exactly like an orphan for a short window, and that ambiguity is not
something to hand an irreversible delete.

The scheduled pass's own destroy step additionally refuses to run anywhere except the production
control plane, regardless of how its flags are set.

Every lever here requires an authenticated superadmin session. Treat that session the way you
would any credential with this much reach — do not leave one open on a shared machine.

## Where to go next

- `reclamation-escalation.md` — what to do when the reclamation pass reports a container it could
  not judge.
