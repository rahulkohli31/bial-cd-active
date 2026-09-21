# ADR-0029: Azure Is the Fleet of Record — Tiered Sandbox Reclamation

## Context

Every build sandbox is a container app. The only record that a container belongs to somebody is a
coordination-store hash keyed by user id, and that hash is the one key family in its namespace
with no expiry. When it is lost — a restart, a flush, a failover — the container becomes
anonymous: unreachable by the product, invisible to every automatic path, and billing for as long
as nobody notices.

This has happened at least twice, with one container still running after 19 days. Both incidents
were misdiagnosed as scheduling failures, because documentation across the codebase asserted that
no scheduler existed for this — it did, a sweeper had run on a short interval since early in the
project. The sweeper cannot fix this, and no amount of tuning would: it asks the coordination
store which containers it owns and acts on the answer, so a container the store has forgotten is
not in the answer and the sweeper never sees it. The store that fails is the store being trusted
as the source of truth. The fix is not a better sweeper on the same authority; it is to change
which system is authoritative.

## Decision

**Azure is the fleet of record; the coordination store is a spare-list only.** Reclamation
enumerates Azure as the authoritative fleet and reads the coordination store only to build a list
of containers to spare — never a source of deletion targets. Under the old direction,
unknown-to-the-store meant run forever; under this one it means evaluate for destruction, safe
only because every gate below stands between "candidate" and "destroyed." This is the same shape
the per-project-database reconciler already uses: enumerate the external system, diff against the
registry, classify into named buckets with a stated total invariant. Enumeration is scoped to the
platform's own resource group, and reads Azure's live resource-management API rather than its
eventually consistent search index — a resource missing from an eventually consistent index would
read as "no orphan," the exact failure being fixed, reintroduced at the query layer.

**Identity is stamped on the resource, at creation, by the platform.** A container must be
judgeable without the coordination store, so identity lives on the resource as tags, written into
the creation request so they exist from the first moment: resource kind (a build sandbox, distinct
from a deployed app), owning user, serving app, the control-plane environment that created it (so
a development environment can never judge a production container), a self-stamped creation time,
a marker for a synthetic backfilled age, and a two-pass staging marker written on one pass and
read on the next. Creation time is self-stamped, never read back from the resource, since the
platform's behavior on reusing a name is undocumented and a retained original timestamp would read
as permanently overdue. Tags sit outside both revision-scoped and application-scoped configuration
so updating them touches neither — the container's application configuration is the durable home
of its own supervisor credential — but a full-replace update still strips tags absent from its
body, so every code path that issues one must include them explicitly. An untagged container has
no tag set at all, never an empty one; parsers treat that as the untagged population it is.

**Confidence tiers, not a single age threshold.** How long an unclaimed container waits is set by
how many independent signals concur, not one duration — a single threshold defends only against
"created but not yet recorded," a window the provisioning retry policy already bounds at roughly
twenty minutes, and does nothing against a lost or wrong store. A container carrying platform
identity, absent from the spare set, with no matching app record, and already staged on an earlier
pass is destroyed at one hour; the same container with a matching app record — a real builder's
real app whose ownership record alone is gone — waits four hours, one fewer concurring signal for
a longer wait. A *registered* container whose lock, stay-of-execution and liveness lease have all
lapsed runs through the identical durable-copy, staging and destroy chain at roughly the
twenty-minute threshold — this path accounts for nearly all of the reclaimed cost and is exempt
from none of the gates the others are subject to. Any signal that cannot be read — identity
predating tagging, an unreadable liveness signal with no confirmed durable copy, an unreachable
product database — is never destroyed and is reported instead. Registration alone does not spare a
container: an entry deliberately survives a completed turn, so a pardoned-then-abandoned container
would sit in a naive "registered means spared" set forever. The spare set is registered *and*
(an actively held lock with a live heartbeat, or a current stay-of-execution, or a held liveness
lease, or a bounded just-starting window) — reverting to "registered is enough" would silently
disable nearly all reclamation while every other test stayed green, so it is guarded against that
exact regression.

**An unreadable signal escalates; it never expires into a decision.** A fact that cannot be read
does not become true by waiting — the same lesson a prior incident in this codebase paid for, where
a readiness timeout was treated as a death certificate and took a destructive branch. Only positive
confirmation of death may take a destructive branch; everything else degrades to retryable or to
escalation. A rate-limited enumeration call raises rather than returning a short list, since a
partially enumerated fleet must never read as a clean one; a delete reporting success for an
already-absent resource is idempotent success, not proof this pass destroyed anything; a product
database that cannot be reached escalates the whole pass rather than treating every container as
record-less. The store-fault guard is proportional, not binary: the coordination-store hash is the
one key family with no expiry, so under eviction pressure the lock, stay and liveness signals can
be evicted first while the hash survives, leaving a live build looking exactly like a lapsed claim
with a non-empty spare list — a guard that only checks for an *empty* list would miss it entirely.
The guard instead trips when the spare set is anomalously small relative to the live fleet, more
conservative than a literal reading requires, because a false positive only escalates to a human,
the correct direction to be wrong in.

**Two independent reads, a full interval apart, plus a ceiling.** Nothing irreversible happens on
a single observation: a container is stamped with a staging marker on one pass and may be
destroyed no earlier than a later one. The marker lives on the resource itself, not a database
row, so it survives the same restart that loses the coordination store, and an operator can verify
the rule without the platform's help. The minimum staging age is a full pass interval, not merely
"staged on an earlier pass" — reclamation is also operator-triggerable, so two back-to-back manual
runs would otherwise satisfy the rule with no time elapsed between the two reads it requires. The
pass runs every fifteen minutes, load-bearing in three places at once: the staging interval, the
effective lifetime of an abandoned container, and the unit of the pass-staleness alarm. Staging
applies to both paths with no exemption — one was considered for the lapsed-claim path and
rejected, since at this cadence it would buy only minutes of container life against a carve-out in
the one rule that protects against a transient misjudgement. Effective lifetime for an abandoned
container is therefore roughly its own claim's lapse window, plus up to one pass interval to
detect, plus one more of staging — on the order of an hour. Each pass also has a destroy ceiling:
on reaching it the pass ends and reports the remainder, bounding both the blast radius of a
misjudgement and the pass's own runtime against the platform's short graceful-shutdown window.

**Destruction ordering is the opposite of the per-project-database reconciler's, for a stated
reason.** The sequence is four steps — mark the registry entry as ending, tear the resource down,
delete the registry entry, then release any lock on it — and every step matters out of process.
"Azure before the record" is a property of this sequence, not a substitute for it: a failed
destruction leaves the record in place so a later pass retries, and dropping the record on a
failed delete is exactly how an invisible container gets manufactured. The database reconciler
drops its registry row first and destroys the resource afterward, best-effort, because that
resource's name is exactly reversible from its own id with no lookup. A sandbox's name is
deliberately truncated to fit a platform name-length limit and is therefore lossy — it does not
identify its owning app alone and must be matched back against the app table — so dropping the
record first and losing the delete would leave no lossless path back to an owner. Tagging closes
this gap going forward, but the ordering is retained regardless: it is a correctness property
independent of identity, and every container created before tagging shipped remains lossy forever.

**Nothing is destroyed without a confirmed-current durable copy.** If the newest durable copy
predates the newest change, one is taken first; if that cannot be confirmed, the container is
spared and reported instead. The gate checks the platform's own autosave-at-turn-boundary copy,
not a separately saved one, matching the rule that reclamation protects a builder's last completed
change, not everything they ever did. Currency is compared by commit rather than timestamp, since
storage stamps modification time in whole seconds and a save and an autosave in the same second
are otherwise indistinguishable. Both branches of this check need a credential to read the
workspace with, and neither an orphaned nor a lapsed-claim container's path starts with one —
recovery reads a running container's own supervisor credential out of its environment, needing
only its name; if that recovery itself fails, a present and parseable durable copy counts as
confirmed current, and if there is no parseable copy either, the container escalates. This is
signed as an accepted risk below. Separately, an unconfigured storage deployment must read as
"cannot confirm" rather than "confirmed nothing to preserve" — an existing helper's "absent"
answer is correct for its other caller but would be catastrophic here, so the worker's own
settings profile requires object storage to construct at all, making the misconfiguration
unreachable rather than merely discouraged.

**A wall-clock liveness lease, published by the process actually building.** Until a
cross-process liveness signal existed, no process but the one running a build could safely destroy
a claimed container — an in-process "who is live" set is empty everywhere else, so moving
reclamation out of the request path without one would destroy live builds on its first pass. That
lease now exists: the turn-execution engine renews it for the duration of every turn, and
reclamation reads it before the lock/heartbeat pair. It is timer-driven, renewed on wall-clock
time since a monotonic clock is meaningless across processes, carries its own expiry — the
coordination-store hash's lack of one is the root cause this design exists to correct — and fails
closed on an absent or absurd value. A timer may not destroy on an ambiguous reading, but a live
request path must still take a container back decisively: a turn killed mid-build leaves a live
lease behind, so the certified-dead path clears it, or the same builder's next start would be
refused until it expires on its own. Evidence of use is weighted toward what happens inside the
container over what happens at a keyboard — a turn in flight outranks every other signal, the app
actually being requested is next, and a builder's own explicit actions earn a bounded extension
after that. Typing, a visible tab, or a held-open connection are deliberately not evidence on
their own. A platform-service health check is excluded on the same principle and on measurement:
it enters through the same path as user traffic and reads non-zero even on a container abandoned
for weeks.

**Where the work runs.** Reclamation runs as a scheduled task on a background worker process,
sharing an image with the control plane but started in a different role. Three properties belong
to it specifically. It is idempotent and single-flighted on a database advisory lock, not one held
in the coordination store: exclusivity across the worker's own deploys cannot be guaranteed, since
the platform drains an old instance while a new one starts and the queue library elects no leader,
and a coordination-store lock key evicted mid-pass would silently undo single-flight inside the
destructive chain itself. A destructive task is never redelivered — no retry policy is attached,
and receipt is acknowledged before execution — because at-least-once delivery buys nothing here (a
lost pass is simply re-driven by the next tick) and would otherwise risk an unbounded destructive
retry loop against a task that crashes its own worker. And a dead worker must be distinguishable
from a quiet fleet: every pass records its own completion, outcome and counts to the database
rather than the coordination store, and staleness of that record — its *absence*, not its
content — is the operator's primary liveness check for a process with no readiness probe of its
own, closing the original incident's failure one layer out.

**It ships report-only, with a written exit condition.** Reclamation ships with destruction off,
logging what it would destroy and why, and only begins destroying once its judgement has been
checked against the real fleet — three consecutive passes whose candidate set a named human agrees
is genuinely abandoned. Report-only without a written exit condition is a delay dressed as
caution. Two things must also be true first: the lapsed-claim tier is suppressed from report-only
output until the liveness lease is read everywhere it needs to be, since before that its spare set
has no writer and every build running past a few minutes would misreport as a candidate; and the
tag backfill must report zero untagged containers before destruction is enabled, since an untagged
container is escalate-only forever otherwise — including the very ghost this design exists to
collect.

## Consequences

A forgotten container is now collectable: the failure that produced a weeks-long, dollars-a-day
ghost twice moves from invisible until a human goes looking to classified on a fixed cadence with
the evidence behind the verdict written down. An operator can answer, from the platform itself,
which containers exist, who owns each, and why any of them is a candidate. The platform now runs a
second long-lived process with destructive authority — a new operational surface with its own
liveness question and its own identity grants. Report-only is a real phase with a real exit, so
the cost saving is deferred by at least three passes plus a human review, deliberately.

## Rejected alternatives

A better sweeper on the same authority was rejected: it still asks the store that fails which
containers exist. An eventually consistent resource index was rejected for enumeration, for the
same reason motivating this design. The resource's own recreate-under-the-same-name timestamp was
rejected as an age source, since that behavior is undocumented and a retained timestamp could read
as permanently overdue. A database row was rejected as the staging marker, since it would not
survive the restart this design defends against. An opaque owner reference resolved by a database
lookup was rejected, since judging a container without the store is the requirement a lookup
defeats. A coordination-store lock was rejected for single-flight, for the reason the whole design
exists. Exempting the lapsed-claim tier from two-pass staging was rejected as a carve-out not
worth the minutes of container life it would buy. Sharing the control plane's own managed identity
with the worker was rejected for this change specifically, since nothing in the existing
credential path names an explicit identity and converting it risked breaking sandbox provisioning
and image builds at once — the worker gets its own identity instead.

## Accepted risks

The reclamation worker holds both delete authority over the fleet and the ability to read
supervisor credentials out of running containers — two capabilities previously separate, now in
one scheduled, unattended process. Acceptable because token recovery is what makes the
durable-copy gate satisfiable on the orphan path at all; the token is read for exactly one
purpose, the process runs no user-supplied code, and its identity holds narrowly scoped roles
rather than a subscription-wide one — but a compromise of this worker is a compromise of every
live sandbox's supervisor channel, stated plainly rather than left implicit. The owning user's id
is stamped in plaintext on the resource, readable by anyone with visibility into the resource
group and surfaced in cost reports; acceptable because judging a container without the store rules
out an opaque reference needing a lookup, and a user id is not a credential. The single-scheduler
property is a defence, not an exclusivity guarantee — two schedulers coexist during every deploy
window, and the pass's idempotence and single-flighting on a database lock is the property that
actually holds. The coordination store's eviction policy is a provisioning gate, not a verified
fact, since the instance is shared infrastructure and changing it may not be grantable; acceptable
because the proportional store-fault guard above is designed for exactly the partial-loss shape
that produces. A two-hour age ceiling applies unconditionally, on top of every tier above, to a
container no claim signal ever disqualifies — a builder who leaves a tab open without acting for
that long loses the container and gets it back transparently on the next prompt; accepted as a
bounded, designed-for cost against an unbounded one, and measured from the container's own age
specifically so a stale coordination-store record cannot hand it a fresh ceiling it does not
deserve. The pass-staleness alarm and the fleet-size alarm are, today, only distinguishable log
events — pull-mode signals that still require a human to already be watching, close to the
original incident's own failure mode. This is accepted only as a stated partial: it is not met
until an actual alerting rule and a named recipient exist for each, and must not be read as met on
the strength of the log line alone.

## Related

ADR-0011 (the queue and worker topology this design adopts), ADR-0028 (the per-project-database
reconciler this one is modelled on, and states the destruction-ordering asymmetry against),
ADR-0014 (the sandbox environment a second process now holds delete authority over), ADR-0015
(deployment — now a two-run-target topology from one image), ADR-0005 (the backfill and reconcile
endpoints audit counts only, never names), ADR-0006 (a user id on a tag is an identifier, not a
secret).
