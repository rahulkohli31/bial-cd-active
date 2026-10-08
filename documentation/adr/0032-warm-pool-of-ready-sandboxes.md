# ADR-0032: A Warm Pool of Ready Sandboxes

## Context

A start that finds no workspace running makes the person wait about a minute: reopening a saved
app, a new project's first message, a chat message after the workspace has gone, switching
projects, and a colleague opening a shared app. About twenty-two seconds of that is Azure creating
the container — placing it, pulling the image, booting it, passing its health checks — and the
platform does not control it. The rest is work the platform does once the container exists:
restoring the project, starting the dev server, the page's first compile, the browser noticing.

A container made ahead of time would take the create out of the wait. The code did not allow one,
because a container was tied to its app from the moment of its birth, in three ways. Its name was
derived from the app's id. Its project settings — the app's identity, the storage link and key,
the database address, the connector coordinates — were fixed in its environment when it was
created. And the supervisor inside it had no way to receive anything afterwards. Changing a
container's environment through Azure instead is no way round it: that creates a new revision and
restarts the container, which costs close to a create and would cancel the saving.

The derived name also did harm of its own. Every place that found a live container — the preview
address, the sweep, the shared-view checks, the share revoke — recomputed the name from the app
instead of reading a record. And a delete issued by name could remove a replacement created under
the same name moments later, which is why the delete of a container holding a person's slot ran
inside their request, about half a minute of it, rather than in the background.

## Decision

**A pool of ready containers, each used once and each holding nothing that belongs to a
project.** A pool container is created, booted and healthy, with no app identity, no storage
credential, no database address and no data identity. When it is claimed it serves one start and,
when that project leaves, is saved and deleted exactly as a container created on demand is. It is
never returned to the pool: generated code has run in it, so reuse would risk carrying one
project's files, packages or processes into the next.

**Every kind of start claims from it, at the one place every start already creates a container.**
Reopening, a new project, a chat message, a switch and a shared view all pass through the same
create step, so one claim there covers all of them, and no start kind or project type is treated
differently. If nothing ready can be claimed, the same step creates a container the way it always
has, so a failure in the pool can never fail a start that would have succeeded without it.

**Settings reach a claimed container through the supervisor, once, after the claim.** The supervisor
accepts one new authenticated call that takes the project's settings. It is limited to the
project's own names among those the supervisor already allows into a generated app's environment,
accepted once per supervisor process, and it never echoes or logs what it was sent. The call is
authenticated with the container's own bearer token — the credential the platform already holds
for every sandbox, read back from Azure by name at the claim, because the process that made the
container is often not the one that claims it. The values arrive in the supervisor's own
environment, so the child-environment allowlist and the redaction of command output and logs treat
them exactly as they treat settings given at creation, with no second code path to keep correct.
A pool container reports whether it has been configured and refuses to start the app until it has.
A claimed container that Azure later restarts comes back from its creation settings, so
unconfigured, and with none of its files. A start or a chat message that would attach to it reads
that report and treats it as gone: the saved copy is restored into a new container, and the
restarted one is deleted with nothing written back.

**A claim is checked before it is trusted.** After the claim, one health call, bounded at a few
seconds, far under the time of a create. A container that fails the check, or any later step of the
claim before its registry write, is torn down and one more ready container is tried — except one
whose bearer Azure would not read for a moment, which goes back to the pool untouched; after two
failed claims the start creates a container as before and records why. The registry write — a
claim's and a create's alike — lands only while the person's record names no container. If another
start has taken the slot meanwhile, or the coordination store does not answer, the start fails as a
create's would, spending no further ready container. Otherwise the person sees nothing different
from a start that never had a pool.

**A container's name is random, never derived and never reused.** Every new container, whether
made for the pool or on demand, is named from a secure random token in the shape the web edge, the
supervisor and the fleet listing already accept (ADR-0006). Two consequences follow. A delete by
name can only ever remove the container it was issued for, which makes it safe to run the delete of
a slot-holder in the background, as switching projects already did. And the preview address follows
the container: the address handed out for a start leads to that start's workspace, and an address
from an earlier one never reaches a later one. No screen offers a workspace's preview address to
keep, so nothing depends on it staying the same.

**The registry is the record of a live container; a small ledger holds only what no registry
records yet.** The registry — the coordination store's per-user record of the
container a person holds — now also records the app's id. Every lookup compares the recorded app
and the recorded shared-view fields, through one comparison and one classifier, instead of
re-deriving a name. That is what lets a container carry any name, and it removes a quiet hazard:
telling a shared view from a build sandbox by the prefix of its name decided whether a colleague's
copy would be written over the owner's saved work, and whether a removed colleague kept a running
copy of the owner's app. The pool's own state lives in the platform database, one row per pool
container: its name, its address, the image it was made from, a state (filling, ready, claimed,
retiring) and the time of the last change. A start that creates its own container writes a row for
it too, claimed from the outset, because a create outlives a cancelled start and until the registry
records the container nothing else names it. When the registry write succeeds, or the container is
confirmed gone, the row is deleted, and from then on the registry describes the container like any
other. The ledger is in the database, not the coordination store, for the reason ADR-0029 records:
the coordination store can lose what it holds, and a container nobody records keeps billing. An
exclusive hand-over between processes also needs a lock the database provides.

**The claim is a compare-and-set on the ledger.** One statement locks a ready row, skipping rows
another claim already holds, and marks it claimed: rows made from the current image first, rows
from an older image only when none of those is ready. Two starts never receive the same container,
even through the overlap of a deploy, when two instances of the backend briefly run side by side.
Every retire is the same kind of statement, conditional on the state the row was read in, so the
retire of a ready container can never take one a start has just claimed.

**Age, owner and kind come from the claim.** A pool container is created carrying only its kind,
the control-plane tag and a pool tag. The claim's registry write stamps its creation time, which
the sweep already reads from the registry when the Azure tag is absent, and a detached restamp then
writes the kind (shared, for a shared view), owner, app and creation tags. A restamp that is lost
is not repaired: the sweep judges from the registry.

**Refill is in the backend, the count is held by the worker, and each process bounds its own pool
work.** A claim starts one replacement as a detached task in the backend, beside the restamp
above, so refill never waits for a periodic pass. A worker task every minute, under the database
lock that stops two schedulers acting at once, brings the count to its target. That pass is the
only periodic one: a backend runs none, even when it starts, so after a backend start the pool
changes only through claims and their refills until the worker's next tick, at most a minute
later. The pass deletes the container of a filling or claimed row left past its deadline (longer
than a create and a new container's first answer can take, timed from when the fill gets its
turn) — though a claimed row whose container a registry record or an
owed deletion names has become somebody's workspace, and loses only its row — and retries retiring
rows. It retires ready containers above the target, older images first. Then, unless a filling row
was past its deadline, it fills one container at a time and stops at the first that is not made.
It acts only on containers the ledger holds and never compares the ledger with Azure's list, so a
container waiting for its save is never touched. A dead ready container is found by the next claim,
let go, and replaced by a later pass. A filling row is written before every create, and only while
the configured image's filling and ready rows are below the size, so refill and the worker count
each other's creates in flight and never make more than the size between them. A container made
for the pool is marked ready only once its supervisor answers, which can be a minute and more after
Azure reports it made; one that has not answered within minutes is deleted like a refused create.
Creates, deletes and restamps share a bound of two at a time in each process, which leaves that
process's own Azure worker threads free for live starts.

**Size follows the working day, in India time.** Each environment sets a day size, a night size,
the day's hours and the day's days; zero is a valid size, and dev may hold none at night. Each size
has a ceiling of twenty, and a value above it stops the process from starting, so a mistyped size
fails a deploy rather than quietly running dozens of containers. At the change from night to day
the worker's pass fills the difference, so the pool grows from the day's start; an operator who
wants it full at opening sets the start a few minutes early. The platform serves one organisation
in one country, so the zone is a constant in the code and not a setting.

**An image deploy replaces ready containers new before old.** Each ledger row records the image it
was made from. When the configured image changes, the worker fills replacements from the new image
first and retires an old-image ready container only when a new-image one is ready to take its
place; until then an old one can still be claimed, so the count never dips during the swap. The
swap begins at the worker's first pass under the new image reference. Only unclaimed containers
are swapped. The platform learns that an image was deployed only from the image reference
changing, so each sandbox deploy sets it to the new immutable tag in both the backend and the
worker; a moving tag would hide the deploy.

**The pool ships switched off.** Every size is zero when this lands. A connector project's app
reads tenant data through an identity that Azure attaches when the container is created, and a
ready container has no project yet, so it cannot carry it. Until a follow-up gives a connector
project's claim that identity — by attaching it after the claim, or by a second small pool created
with it — a ready container that a connector project could claim without it must not exist, and no
condition by project type is added to make an exception. The sizes are raised once that has
shipped.

## Consequences

- A start that claims skips the Azure create, about twenty-two seconds measured on the platform's
  own environments. Everything after it — the restore, the dev server, the first compile — is
  unchanged, so how much a person saves is read from the per-start records, not assumed. Every start
  is recorded once with whether it claimed and, if not, why; the records are read only as
  superadmin aggregates.
- A start the pool cannot serve is today's start, unannounced and recorded with its reason: none
  ready (more starts than ready containers, or Azure refusing creates), a size of zero, a claimed
  container that failed its health check, or a later step of a claim that failed.
- Ready containers cost money. They are billed as running containers, and whether a lower idle
  rate applies in this tenant is not established. The size settings are the lever, the night size
  keeps the idle cost down, and an image swap briefly doubles the pool inside the environment's
  capacity of addresses and cores, which the ceiling exists to bound.
- A claimed pool container never has its project's database address or storage key in Azure's own
  record of it, because they were delivered over the supervisor's channel after the claim. A
  container created on a miss still carries them there, as every container did before.
- **This amends ADR-0029 twice.** It says identity is written into the creation request so that
  every container is judgeable from the first moment; a pool container is created with only its
  kind, control-plane and pool tags, and owner, app and creation time are written at the claim, so
  between fill and claim a container with a pool tag and no owner is expected. And it says the
  worker's identity holds narrowly scoped roles: the worker now creates containers, tags included,
  so its identity needs the container-apps write action, which creates a container with its tags,
  and the environment-join action on the sandbox resource group — actions the control plane's own
  role already holds. The worker still runs no user-supplied code and creates containers only from
  the platform's own image, but a compromise of it is now also the power to create billable
  containers there.
- The worker builds each pool container's environment, including the portal's origin, so it has a
  required setting for that origin, validated as the API validates it.
- The orphan inventory and the tag backfill's unowned count leave out containers the ledger holds,
  a container a start is still creating among them, and containers whose deletion is still owed.
  The sweep needs nothing for the pool: it reaches only containers a registry names.
- The ledger describes containers that belong to nobody until the registry records them, and deletes
  its row when it does, so it carries no user id; ADR-0004's rule scopes data that belongs to a
  user, and the claimed container's record is the registry, scoped by user as before.
- A record written before this change has no app id, and a shared view's owed-deletion row that an
  older process writes still asks for its tree to be written back; both are read by the container's
  name instead, and a later release removes that fallback.
- **The release is roll-forward only once a container has been created under a random name.** The
  newer code handles everything an older release leaves behind; the reverse loses work. An older
  sweep cannot map a random name back to an app and deletes the workspace without saving it, an
  older backend opening any project deletes the person's current workspace without saving it, and
  an older retry of a deletion the newer code owes writes a shared view, or a discarded container's
  tree, over an app's saved copy. So the release is deployed outside working hours in an order that
  keeps old and new processes from meeting, with the sweep on throughout. A forced rollback first
  saves and ends every live workspace through the new code with every pool size at zero, waits
  until no deletion is owed, and only then starts the old code; the procedure is in
  `runbooks/reconcile-and-reclamation.md`.

## Rejected alternatives

- **Reusing a released container for the next person.** Rejected for the reason under the first
  decision: generated code has run in it.
- **Delivering settings by changing the container's environment through Azure.** It restarts the
  container, close to the cost of a create.
- **Parking each app's own container and restarting it.** A restart still pulls the image and
  boots, and new projects and shared views gain nothing from it.
- **Keeping the name derived from the app and looking it up at the web edge.** Every preview
  request would pay a lookup, to preserve an address nothing hands out. ADR-0033's cached lookup
  exists for a different reason: an address that does not name its container.
- **A full ledger of every container, beside the registry.** Two records of one fact. The registry
  gains the app id and the ledger covers the pool alone.
- **A reconciler that lists Azure and deletes what the ledger does not hold.** A container the
  ledger does not hold may be a person's switched-away workspace waiting for its save, or one
  created seconds ago whose registry write has not landed — the ambiguity that keeps the orphan
  inventory report-only. Acting only on ledger rows removes the question.
- **Retiring the whole pool on an image deploy and then refilling.** Simpler, but every start for
  about one refill after each sandbox deploy takes today's path. New before old costs capacity for
  twice the pool for a few minutes, and keeps the count steady.
- **Pausing the sweep during the release's deploy.** The sweep is what deletes abandoned
  containers; the deploy order does the separating instead.
- **Azure's custom-container session pools as the runtime.** As documented, they require a bearer
  token on every request, which a framed browser cannot present; hold their environment at pool
  level where session code can read it; discard the filesystem when a session's cooldown ends; and
  bill a standing node floor whatever the use.
- **Azure Container Apps sandboxes.** A preview whose terms carry no service level and exclude
  production use, with the tenant's region unconfirmed. To be reconsidered when it is generally
  available with a written support position.
- **AKS with pod sandboxing.** A second orchestration platform to run and secure for one workload a
  managed platform already serves. Reconsidered if a hard isolation requirement appears.
- **Sizing the pool from demand.** Sizes are operator settings; automatic sizing needs arrival data
  the per-start records have yet to gather.

## Accepted risks

A pool container's supervisor bearer sits in Azure's environment record for the whole of its wait,
readable by the same identities that can read any container's today. The wait is bounded by the
image swap and the night shrink; the exposure is the one ADR-0029 already accepted for every
sandbox, held for longer.

A crash or a cancelled start between a miss's create and its registry write leaves a randomly named
container that no registry record names. The ledger row written before the create still names it,
so the orphan report counts it as held rather than as an orphan, and the worker's pass deletes it
once no create could still be running; until then, under half an hour, it keeps running and bills.
A start whose ledger write fails creates all the same, and such a container is left to the orphan
report.

The environment's capacity for twice the pool on top of peak live workspaces is not verified. The
ceiling bounds the exposure, and capacity is to be checked before any size is raised.

The below-size alarm is a log event, like the platform's other alarms, and is met only when an
alerting rule and a named recipient exist for it. It must not be read as met on the strength of the
log line alone.

## Related

ADR-0004 (the isolation rule, and why the pool's ledger sits outside it). ADR-0006 (the random token
behind every name). ADR-0008 (the native enum that holds a ledger row's state). ADR-0011 (the queue
the per-minute task rides on). ADR-0014 (the sandbox and supervisor this pool makes ahead of time).
ADR-0029 (amended above) and ADR-0030 (the sweep, which reaches a pool container only once a claim
has recorded it).
