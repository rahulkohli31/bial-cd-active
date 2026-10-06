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
| `POST /v1/admin/apps/reconcile-sandboxes` | Azure + Redis + the database | — | never | you want the orphan inventory — containers Azure is billing for that no registry record tracks and nothing else the platform holds accounts for |
| `POST /v1/admin/apps/backfill-sandbox-tags` | Azure + the database | Azure resource tags | never | a fleet predates identity stamping: an untagged container carries no owner or age the sweep can read off ARM when Redis cannot be trusted |
| `POST /v1/build-sessions/internal/reap` | Redis + Azure | Redis | **yes** | you want to run the platform's routine abandoned-sandbox sweep right now, across the whole fleet, instead of waiting for its next scheduled pass |
| `POST /v1/admin/apps/reconcile-deploys` | the database + Azure | the database | never | a deployment row is stuck because the process driving it died mid-pipeline |

## Which lever answers which question

- **What is leaking?** `reconcile-sandboxes`. Its "unregistered" count is the leak: containers
  Azure is billing for that no registry record tracks, so the scheduled sweep — which only ever
  reaches what the registry still names — will never reach them either. A ready sandbox, a
  container whose deletion is still owed and one a start is still creating are not leaks, and are
  left out of it (see the last section).
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

Only the reap lever above and the scheduled sweep ever delete a container that somebody was using;
everything else here only reports. The pool's own pass, every minute in the worker, also deletes
containers, but only ones its own ledger holds, never one a registry record or an owed deletion
names. The reporting design is deliberate, not an unfinished feature: a container provisioned
seconds ago can still look like an orphan for a moment — its record moving from the pool's ledger to
the registry while the report reads them, or a start whose ledger write failed — and that ambiguity
is not something to hand an irreversible delete.

The scheduled sweep runs two passes: first the same registry sweep the reap lever runs, then a
retry of any container deletion the platform still owes — a container a switch or a start let go
of whose delete has not been confirmed. The registry no longer names those containers, so the
first pass cannot reach them. The reap lever runs only the first pass; the owed-deletion retry
runs only on the sweep's own schedule.

On that schedule the registry pass runs only on the production control plane, regardless of how
its flags are set; the reap lever runs it on demand anywhere. The owed-deletion retry runs on every
control plane, because it only carries out deletions a start or a switch there already decided,
and off production nothing else would ever retry one.

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

Never do this to a ready sandbox the pool holds; drain the pool instead (next section).

## The pool of ready sandboxes

The pool keeps sandboxes made ahead of time so a start can take one instead of waiting for Azure to
create one. `architecture.md` explains why it is shaped as it is, and `taskiq-worker.md` section 5
says what its per-minute pass does. None of the four levers above manages it: its controls are its
size settings and the procedures below.

### Draining the pool

Draining removes every ready sandbox and makes no more. A sandbox somebody is using is never
touched. Do it when the alarm below cannot be fixed, when spend must stop at once, and before a
forced rollback.

1. Set every pool size, day and night, to zero — in the worker first, then in the backend — and
   restart each so the change takes effect. The worker goes first because only its pass retires
   ready sandboxes; the backend runs no pass of its own. A backend that still holds a size
   meanwhile can claim what is still ready, and the worker retires the replacement each such claim
   makes.
2. Wait for the worker to retire what is ready. One pass retires everything above the size, one
   sandbox after another, each delete waiting for Azure to confirm it, so a large pool takes a few
   minutes. A delete Azure refuses leaves its row retiring, and the next pass tries it again.
3. Confirm that none remains. In the platform database the pool's ledger holds no row that is
   filling, ready or retiring:

   ```sql
   select state, count(*) from sandbox_pool group by state;
   ```

   A claimed row for a start still in flight — a claim, or a start creating its own sandbox — may
   show while it runs, and resolves itself. A row that stays retiring means Azure keeps refusing
   the delete: check that the worker's identity may delete containers there. Do not judge from
   Azure's tags instead: a claimed sandbox whose tags were never restamped still carries the pool
   tag and no owner, and it is somebody's workspace.

Drain through the settings, never by deleting containers by hand. A container deleted by hand
leaves its ledger row behind as ready. While a size is above zero, the next start to claim it finds
it gone, lets it go and tries one more; after two failed claims it creates its own, recorded as a
failed claim. At size zero the next pass clears the row.

To fill the pool again, restore the sizes in both processes and restart them. The worker's passes
fill it one sandbox at a time, which takes a few minutes for a handful.

### The below-size alarm

It means a pass ended with fewer ready sandboxes than the size, and something stopped it filling:
Azure refused a create, a new sandbox never answered, or a row sat filling past its deadline. That
pass made no further create; the next, a minute later, tries again. It does not mean a start
failed. A start
that finds no ready sandbox creates one as it always did, only slower, and its record says why. It
never fires while the size is zero, and like the platform's other alarms it only reaches a person
if an operator-owned rule turns the log event into a notification.

1. Read the worker's log for that pass. A refusal Azure gave is logged beside it.
2. Match it to a cause:
   - **A refusal about authorization.** The worker's identity lacks the container-apps write or
     the environment-join action on the sandbox resource group. Grant them (`../deployment.md`).
   - **A row left filling past its deadline.** A process — the worker or the backend — stopped in
     the middle of a create, usually in a deploy or a restart; the pass that found it deleted the
     container, and the next one fills again. Once after a restart is expected. If it repeats,
     confirm that the worker and the backend hold the same image reference, that the registry has
     that tag, and that the registry credentials in the worker's settings are right, then read the
     container's own log in Azure.
   - **A sandbox that never answered.** Azure made it, but its supervisor did not answer within
     minutes, so it was deleted. Check the image reference and the registry credentials as above,
     then read the container's own log in Azure.
   - **A refusal about capacity or quota.** The environment is full. Every ready sandbox takes
     addresses and cores on top of those in use, and an image swap briefly needs twice the pool.
     Lower the sizes, or raise the capacity first.
   - **A refusal that clears by itself.** Nothing to do; the next pass restores the count.
3. If it cannot be fixed soon, drain the pool. That silences the alarm and stops the spend, and
   starts carry on as before.

### Deploying the release that introduced the pool

That release changed how every workspace is named and found, and an older release meeting its
records loses work. In a measured run of the two side by side, the newer code handled everything
the older one left behind. The reverse failed every way it was tried: an older sweep deleted an
idle, randomly named workspace without saving it; an older retry of a deletion the newer code owed
wrote a colleague's shared view over the owner's saved copy, or a discarded container's tree over
its app's saved copy; and an older backend opening any project deleted the person's current
workspace without saving it. So the release is deployed outside working hours, with the scheduled
sweep on and nobody calling the reap lever, in this order and no other:

1. Run the migrations. The older release runs safely on the migrated database.
2. Stop the old backend.
3. Deploy the worker, and wait until only its new revision is running.
4. Start the new backend.

### Roll forward only

Once a container has been created under a random name — which is every start after the release
that introduced the pool, pool or not — the platform can only go forward. Do not start an older
backend or worker against a fleet that holds one. Nothing in the database needs reversing: the
older build ignores what the newer one added.

If a rollback is forced:

1. Drain the pool, so that the older build never meets a ready sandbox.
2. With every size at zero, save and end every live workspace through the new code. The reap lever
   saves each workspace before it deletes it. Run it until `reconcile-sandboxes` reports no
   registered containers. It ends only workspaces whose claims have lapsed — one somebody is still
   working in, or a finished build inside its stay, is spared — so choose an hour when nobody is
   working, and repeat the run.
3. Wait until no deletion is owed: the platform database's `pending_teardowns` table is empty. The
   scheduled sweep retries each owed deletion every five minutes. An older release that retried one
   of these would write a shared view, or a discarded container's tree, over an app's saved copy.
4. Only then start the older code.

### The orphan report leaves out what the platform still holds

A ready sandbox is named by no registry until a start claims it, so `reconcile-sandboxes` would
read it as an orphan. It does not: the report leaves out every container the pool's ledger holds —
a container a start is still creating among them, until the registry records it — and every
container whose deletion is still owed. The tag backfill's count of unowned containers leaves out
both. Such containers count towards the report's live total and are neither registered nor
unregistered in it. A name that does appear in the "unregistered" list is therefore a real orphan,
and the steps under "Deleting by hand" apply to it.

A container left behind by a start that died mid-create stays out of the list while its ledger row
stands. The pool's pass deletes it once no create could still be running, under half an hour after
the start asked for it, unless a registry record or an owed deletion has come to name it by then.
Only a start whose ledger write failed leaves a container that nothing holds, and that one is
listed as unregistered.
