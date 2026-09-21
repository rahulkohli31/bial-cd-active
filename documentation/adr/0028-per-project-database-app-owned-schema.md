# ADR-0028: Per-Project Database with App-Owned Schema

## Context

Each generated app persists its own data. With many untrusted apps on shared infrastructure, one
app must never read or corrupt another's data, and a draft build must never starve a live,
deployed app. This is a second isolation boundary, distinct from the control plane's own
user-scoped isolation: that one scopes the platform's own data by the owning user; this one
isolates each generated app's runtime data from every other app's.

The platform originally intended to author every generated app's schema itself, through a
privileged migration runner, with the app's own database role forbidden from running DDL at all.
The pivot to an open-ended build agent — a real shell, an open write surface, no fixed template —
made that impossible: a runner would have to understand a schema an agent wrote minutes earlier,
for an app the platform has no model of, and every schema change would round-trip through a
platform deploy. Meanwhile the interim design routed every generated app's data through one
shared table, reached over an HTTP API and isolated only by a `WHERE app_id` predicate. That
plane could not sort numerically, aggregate correctly, or transact, and its isolation was one
dropped predicate away from a cross-app leak.

Two external incidents shaped the replacement. In CVE-2025-48757, rows in a shared substrate were
exposed because row-level-security policies an agent was supposed to apply were not applied
consistently — isolation must be structural, not something an agent is trusted to remember
correctly, and the shared table's predicate was the same class of failure. Separately, an agent
holding a production credential destroyed production data in a well-known industry incident; the
right answer — a database created empty at deploy time, with the build agent never holding its
credential — is recorded below as deferred work with a named trigger, not something that ships
today.

A substrate survey ruled out the alternatives. One managed Postgres provider had deprecated its
Azure regions for AWS-only projects with no India region, disqualifying it for data that must stay
in-tenant. Another isolates at the wrong unit — a whole stack of auth, storage and functions per
app — and adds a second vendor. The existing Azure Database for PostgreSQL Flexible Server already
runs in the tenant, has no hard per-server database cap, and already supports password and Entra
ID authentication side by side, so a local password role for the app tier needs no server change.

## Decision

**The boundary doctrine.** The platform owns the database's lifecycle — creating the role and
database, revoking public access, setting per-role timeouts, protecting the credential, dropping
both on teardown. The app owns everything inside it: every table, column, index and constraint,
and the migration history that produces them. Platform DDL stops at the database wall and never
reaches inside; schema and migrations belong to Drizzle, run by the agent in its own shell.
Drizzle's schema-push mode, which applies a diff without writing a migration file, is banned in
the template and the build prompt — a change applied that way would exist only in a live database
and never in the artifact an approver reviews. Migrations are ordinary files in the project's
workspace, so they travel with everything else the agent produces.

**One database per project, for its entire life.** The database is scoped to the *project*, not
the app — storing its markers on the app record would force an app row to exist before a project
is ever built, breaking the platform's fresh-project contract and the "no app row means never
built" invariants that depend on it. It is provisioned at project creation, with a lazy self-heal
at build start for a project created before the substrate existed or while unconfigured. Draft
builds and the deployed app share this one database for the project's whole life — deploying does
not mint a second one, a consequence accepted below as a signed risk, with a dev/prod split
recorded as deferred work. A dedicated registry table holds one row per project; a missing row
means "never provisioned," and an atomic insert-if-absent lets concurrent provisioning attempts
race without double-provisioning.

**Deterministic, full-UUID-bearing names.**

```
database:  bialapp_<project_id.hex>
role:      bialrole_<project_id.hex>
```

Carrying the full 32 hex characters of the project's UUID, not a truncated form, buys three
things: provisioning is idempotent by re-derivation, since a retry computes the same names and a
duplicate-object error reads as success; teardown needs no stored schema, since both handles are
re-derivable from the project id alone; and an orphan reconciler's diff becomes an exact existence
check against the registry rather than a fuzzy pattern match. A name is never interpolated into
DDL directly — a single helper validates a closed character class first, since an identifier
cannot be bound as a query parameter the way a value can.

**Role shape and the cross-app wall.** The role is `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE`,
and owns its own database outright — full DDL inside its own box, no reach outside it.
`NOCREATEROLE` closes a re-entry path a login-disabled role would otherwise keep: minting a
second login role from inside the app. The structural wall is revoking the connect privilege every
new database grants the public role by default, then granting it back only to the database's own
owner, applied as each project's database is created — a grant the platform makes and the app
cannot alter for a database it does not own, not a policy the app has to remember to write
correctly. The same default grant exists on the control plane's own database and had to be closed
there too, in a one-time step outside this per-project sequence, once implementation surfaced that
a project's disclosed credential could otherwise open a reconnaissance session against the control
plane's catalog — no table data, but every table and column name and the full role list. A
database's default schema is also not automatically owned by whoever owns the database containing
it, so provisioning reassigns that ownership to the project's role on a second connection made
before the wall goes up; where the maintenance role lacks the server privilege to do so,
provisioning completes on a non-owned schema and logs the one-time server grant an operator needs.

**Provisioning runs in two phases, in a load-bearing order.** Cluster DDL and a registry row
cannot commit together. Phase one is the claim: an insert-if-absent against the registry's unique
project id, committed immediately so no row lock is held across what follows. Phase two runs on a
dedicated auto-commit connection — database creation cannot run inside a transaction — and
executes, in order: create the role; grant the maintenance identity membership in it with
inheritance and the right to assume it named explicitly, since PostgreSQL 16 stopped granting
these automatically and database creation needs the ability to assume the intended owner; create
the database, owned by that role; reassign its default schema; raise the cross-app wall; set the
per-role statement and idle-transaction timeouts; and stamp a provisioning timestamp into the
database's own comment, the only creation evidence that survives an orphaned database losing its
registry row. Only once every external step succeeds is the registry row's terminal ready marker
written, last, in its own commit — a crash mid-sequence reads as "not ready" rather than "ready
with the wall down," and self-heal re-runs the whole idempotent sequence on any non-terminal row.
Recovery is narrower than birth: a role already present — left by a partial run, or disabled by
the kill-switch below — is converged onto the registry's stored password and its login/create
attributes only, because two of its birth attributes can only be altered by an actual superuser,
which the maintenance role deliberately is not, and nothing else could have changed them since.

**Credentials.** The app tier authenticates with a local password role rather than the control
plane's own identity-based database authentication — a deliberate exception scoped to
generated-app data, since the server already supports both modes side by side. The password comes
from a cryptographically secure token generator, is stored encrypted at rest under a required,
no-default fleet-wide key, and is injected into the sandbox as a single server-side-only
environment variable — never baked into the built image, never published to the browser.
Redaction covers the connection string whole and its embedded password separately, since a log
line printing only the password would sail past a redactor watching for the whole string. There is
deliberately no reset lever on the credential-reveal endpoint: one role serves both the sandbox
and the deployed app, so resetting it would sever a live deployment mid-flight; leak response is a
manual, documented procedure instead.

**No connection pooler in the app path, and a stated connection budget.** App connections go
directly to the server's standard port. A pooler pools by `(user, database)`, so a server-side
session can outlive the client connection that opened it — exactly what would blunt the
kill-switch below, since "revoke and terminate" loses its crisp effect once a pool sits in
between — and a pooler's per-database metrics are capped at a small fixed number of databases, the
wrong shape for one database per project. This is a configuration convention, not an enforced one:
nothing asserts the port. The budget it protects is stated as an inequality —
`Σ(deployed app pools) + Σ(sandbox app pools) + control-plane pool ≤ server connection ceiling` —
concretely, a control-plane pool of 20 plus its driver's default overflow of 10 for 30 per
(single) replica, against a small fixed pool pinned in the generated-app template with the
inequality written beside it and the build prompt told never to raise it. Breaching this
inequality, not "the fleet gets big," is the trigger for a dedicated apps-only database server.

**The kill-switch and teardown share one primitive.** Disabling access runs, in order: disable
login on the role, revoke its connect privilege, then terminate sessions it already holds — the
door locked before anyone is kicked out, so a client reconnecting mid-sequence finds it already
locked. Re-enabling is the mirror image. This is the sole data-plane kill switch for a deployed
app, proven against a live, reconnecting pool, not a single open connection. Irreversible teardown
runs sever, then a forced database drop, then a role drop, after the registry row is already
gone, post-commit and best-effort, so a failed drop becomes a logged, reclaimable orphan rather
than a registry row pointing at destroyed data. The forced drop matters because a relaunched
preview or the deployed container itself can reconnect between the sever and the drop, holding no
lock any guard would see; forcing through that reconnection is the real guarantee. Both delete
surfaces state plainly, before the action, that it destroys the project's database and files
irreversibly.

**Orphan reconciliation is report-only, by necessity.** PostgreSQL hands back no creation
timestamp for a database once its registry row is gone — the provisioning comment is the only
surviving age evidence, and it is easy to lose or never write. So the reconciler only reports: it
enumerates the cluster's databases and roles, diffs against the registry, and buckets each as
owned, orphaned with a parseable stamp, orphaned with no provable age, or — for roles — stranded,
a login role whose database is already gone, the one shape a database-only diff cannot see. A
denylist plus the full-UUID anchor keep every unrelated database on a shared server out of the
actionable set; anything the anchor cannot parse is left alone rather than guessed at. On-disk
size is surfaced beside this as an explicitly advisory number nothing reads as a limit.

**Audit.** Every gated action is audited by role and database name, counts and timestamps — never
the credential. Provisioning is not audited, deliberately: it runs on paths with no human actor to
attribute it to, and a row whose actor is a guess is worse than a structured log line. The
one-time credential reveal the deployment runbook uses returns the connection string in its
response body only — never logged, never audited, never listed.

## Rejected alternatives

A platform-owned migration runner, the original design, required understanding schemas an agent
authored minutes earlier for an app the platform has no model of, and would have routed every
schema change through a platform deploy. A second, frozen database minted at approval was rejected
for this phase — no deployment automation exists to create or seed it, and no way yet to prove a
replayed migration would bring it up clean. Extending the old shared table was rejected outright:
it cannot sort, aggregate, or transact correctly. Schema-per-app on one shared database was
rejected because the wall becomes a search path and a grant inside one database rather than a
connect privilege on a database of its own, and a forced drop has no schema-level equivalent.

## Consequences

Generated apps get a real relational database — transactions, correct aggregates, and their own
migration history shipped in the same bundle an approver reviews. Isolation becomes structural: a
leaked credential reaches exactly one project's database, never a shared table or a predicate an
agent has to remember. Project creation becomes side-effecting against the database cluster, but
lazy-tolerant — a failure leaves a non-terminal marker and a working project. Disabling and
deleting a project now have real teeth against its data, with the reconciler as the safety net for
the window between a failed drop and a human noticing.

## Accepted risks

The credential is disclosed-by-assumption — anything with the preview URL can plausibly obtain
it — which is acceptable because secrecy was never the control: blast radius (one database, zero
cluster reach), fast revocability (the kill-switch above, proven against a live pool), and the
human approval gate before deployment are. The build agent shares the deployed app's live database
until a dev/prod split lands, so a rebuild can alter production data through a migration the agent
authors; acceptable because deployment today is manual and per-app, with a named trigger below.
The maintenance credential reaches every project's database on the server — isolation here is
app-versus-app, not app-versus-platform — acceptable because it is a distinct, minimally
privileged role held only by the control plane, whose compromise is a control-plane compromise
regardless. The fleet-wide key protecting every stored password is a single point of failure,
acceptable because it is required with no default, held with other production secrets, and has a
documented rotation procedure. Disabling a project's database access does not revoke its
still-valid file-storage credential — a pre-existing, already-accepted gap, not a new one.

## Revisit triggers

A dev/prod split — a production database created empty at deploy time, the build agent never
holding its credential — is deferred until deployment becomes platform-driven rather than manual.
A dedicated apps-only database server, with a connection pooler evaluated as part of the same
change, is deferred until the connection budget above actually breaches the tier's ceiling.
Per-project point-in-time restore is deferred because the underlying service restores a whole
server, not one database, and needs to become an explicit client requirement first.

## Related

ADR-0004 (control-plane user scoping — the other isolation boundary, unchanged), ADR-0005 (the
audit vocabulary this follows), ADR-0006 (the token generator and UUIDv7 keys this uses), ADR-0008
(native enums, now an app's own concern inside its own schema), ADR-0013 (the control plane's own
database access, which this runs beside rather than through), ADR-0014 (the sandbox this
credential is injected into), ADR-0015 (deployment — the runbook consuming the credential reveal),
ADR-0018 (the build-agent model that made a platform-owned migration runner impossible).
