# Redis runbook

Redis backs the coordination layer for the build sandboxes: a one-sandbox-per-user lock, an
idle-session heartbeat, and a registry of which container is currently live for which user.
Reach for this document when provisioning Redis for a new environment, when the platform's
health endpoint reports Redis as degraded, or when Redis has lost its data and you need to know
what that actually costs.

## What Redis holds, and what it does not

Everything Redis holds here is short-lived coordination state, not the system of record:

| What | Survives a restart? |
|---|---|
| The per-user build lock | No — it lapses on its own even if nothing clears it |
| The idle-session heartbeat | No |
| The sandbox registry (which container is live for which user) | No |

The durable record of every project, application, conversation, submission and attachment lives
in PostgreSQL and blob storage, never in Redis. Losing Redis interrupts builds in flight; it does
not lose anyone's work. Treat a Redis incident as a cache-loss drill, not a data-loss drill.

There is no publish/subscribe channel here — build progress is delivered in-process on the
control-plane instance that is handling that build. That only works because the control plane
runs as a single instance; see `deployment.md` for why that constraint holds and what it would
take to change it.

## Provisioning

- Use a tier with replication, reached only over a private, encrypted connection. Replication is
  what makes total data loss *rare* rather than routine — it is not a promise it can never
  happen, which is why the recovery procedure below exists.
- The connection is encrypted in transit at the connection-string level, not by a separate
  toggle, and production refuses to start on an unencrypted connection string — on purpose, so a
  misconfigured deployment fails at boot rather than shipping coordination keys, and any
  password embedded in the connection string, in the clear. Do not point an encrypted connection
  string at a plaintext local Redis "to test the encrypted path" — it will not fail fast; it will
  hang for the connect timeout and then raise a misleading timeout error.
- Redis is optional outside production. With none configured, the control plane still starts,
  but build sessions do not work — configure it locally for any work that exercises the build
  path.
- Connection, pool-size and retry settings are declared in `backend/src/services/redis/config.py`
  — read it for the exact settings and current defaults before provisioning or tuning an
  environment. Two things are worth knowing about that code before you touch it:
  - A misspelled setting in an environment *file* fails at startup, which is the safe failure.
    The same misspelling as a raw operating-system environment variable on some hosting
    platforms is silently ignored instead, which reads as "the setting had no effect" — so
    double-check spelling against the config file itself rather than trusting a deploy that
    merely booted.
  - The connection layer installs its own explicit retry policy rather than relying on the
    client library's default, which is effectively no retries at all. If that code is ever
    touched, the retry object must come from the library's asynchronous retry module, not its
    synchronous one — the synchronous one does not raise anything, it just silently stops
    retrying after one attempt while still looking correctly configured on inspection. This is
    guarded by an automated timing test, not by a type check, because nothing about the
    synchronous class's shape is wrong — only its behavior is. Trust the test, not a read of the
    code, if you ever change this.
- With the shipped defaults, the worst-case wait against a fully dead or unresponsive Redis is
  bounded at a few seconds to well under a minute — deliberately far short of what the client
  library's own out-of-the-box retry behavior would produce, which would be unacceptable on a
  request path. Both the startup check and the health probe additionally wrap their own ping in
  a short ceiling of their own, so a black-holed Redis cannot hang either check for the full
  retry budget.

## Reading the health endpoint

`GET /v1/health` reports overall status plus a `redis` field with three possible values:

- `ok` — reachable.
- `unreachable` — configured, but not answering. Build sessions fail; every other route keeps
  working.
- `not_configured` — no Redis configured for this deployment. This is a supported, non-production
  posture, not a fault.

Only the `unreachable` state degrades the endpoint's overall status, and even then the HTTP
status code stays 200 — PostgreSQL is the only dependency that fails this endpoint closed (503).
**Automated consumers must key on the HTTP status code, not on the status word** — a monitor that
treats "degraded" as equivalent to a failed check will fire on every Redis-optional development
box. An operator watching only the HTTP status code will, symmetrically, never see a Redis outage
this way at all: the code stays 200 throughout. Watch the `redis` field directly, or add a check
that exercises an actual build start.

## Recovery: total Redis data loss

Symptom: the lock, heartbeat and registry entries are all gone at once — a flush, a tier change,
a double failover. What actually happens:

1. **Locks.** The next build start acquires a fresh lock cleanly. A genuine Redis outage returns
   an honest, retryable error rather than a false claim that a session is already active, so once
   Redis is back, starts simply succeed. No manual lock cleanup is needed — any lock left over
   from before the loss lapses on its own regardless.
2. **Registry entries.** A sandbox that was live can no longer be reattached by the person who
   owns it — the control plane has lost the mapping from that person to their running container.
   Their source is not at risk: it is restored from the last snapshot in blob storage, never from
   Redis. The container itself, though, is now invisible to the platform's ordinary bookkeeping.
3. **Heartbeats.** In-flight builds lose their liveness signal and are treated as idle. The
   affected users see their build session reset and can simply restart it. Nothing durable is
   lost.

**Operator steps:**

1. Confirm Redis is back via the health endpoint.
2. Expect a wave of retryable errors on build endpoints for as long as Redis is down — that is
   the intended, honest failure, and it clears itself the moment Redis answers. Restarting the
   control plane is not required and will not help; connections are re-established lazily.
3. Interrupted builds are the affected users' to restart. There is no queue to drain and no lock
   to clear by hand.
4. **Containers that lost their registry entry are the one thing worth checking by hand.** The
   orphan inventory endpoint compares the live container fleet against the platform's own records
   and reports containers it can no longer account for. It only *reports*; it does not delete
   anything on its own. Preview what it finds and delete any orphan by hand using
   `reconcile-and-reclamation.md`. A report that comes back empty against a fleet known to be
   non-trivial is a sign the check itself is failing, not proof that nothing was lost.

Because every key here is short-lived and reconstructable, and the platform's durable data lives
in PostgreSQL and blob storage, a total Redis loss costs a handful of interrupted builds and a
short window of retryable errors — never a user's work.

## Where to go next

- `taskiq-worker.md` — the process that shares this coordination store, and its own gates.
- `reconcile-and-reclamation.md` — previewing and recovering the container fleet by hand.
