# ADR-0011: Background Jobs — Taskiq on a Redis Broker

## Context

Some work has to run on a schedule, independent of any request: reconciling a deploy
that never finished, sweeping idle sandboxes, periodically checking the running fleet
against a tiered reclamation policy, and removing conversations nobody has come back to.
That work also has to be able to delete cloud resources and stored records safely, which
rules out the easiest place to put it — inside the request-serving process itself.

Running scheduled work inline within the request process, rather than as a separate
worker, has several problems for exactly this kind of work:

- It is invisible. Nothing records whether a pass ran, so a crashed loop and a quiet,
  healthy fleet look identical from the outside.
- It pins the deployment to a single replica. A second replica means a second copy of
  the same scheduled work running concurrently against the same shared state.
- It cannot be operated. There is no way to trigger a pass by hand, pause it, or see its
  history, separate from deploying new code.
- It shares fate with request serving: a long pass competes with request latency for the
  same process, and a restart interrupts a pass mid-flight.

## Decision

**Taskiq is adopted, with a Redis broker**, to run scheduled reconciliation work. The
queue's passengers are scheduled reconcilers and retention passes — deploy
reconciliation, the sandbox sweep, the fleet reclamation pass, and the conversation
retention pass — not request work moved off the hot path. Long-running
work that a request cannot finish in time follows a different pattern instead: claim a
database row, run the work as a detached task that owns its own database session, and
have the client poll for the result.

**Topology.** One backend image serves two run targets, selected by start command: the
API (`fastapi run src/main.py`, on Azure App Service for Containers, unchanged) and the
worker (`python -m src.worker_main`, on Azure Container Apps, with no inbound ingress).
The worker and the scheduler share a single process rather than two: in Taskiq, the
scheduler is only a clock — on a cron tick it pushes a message, and the worker consumes
and executes it — so a scheduler running without a receiver would enqueue work that
nothing ever runs, a queue growing silently behind a container that looks healthy. The
worker is therefore the process that must exist, and the scheduler rides inside it as a
second supervised task rather than as a second Azure resource. The worker runs on
Container Apps rather than App Service because App Service expects the container to
answer HTTP on its own port and restarts it when nothing does, and a Taskiq worker does
not listen on one.

**The process entrypoint is custom**, not the library's own `taskiq worker` /
`taskiq scheduler` commands. Starting the scheduler from the library's own worker-startup
hook is fatally recursive: the scheduler's startup calls the broker's startup, which
fires the worker-startup event again because it is now a worker process, re-triggering
the handler that spawned it. Relying on `taskiq worker` for single-scheduler safety
fails the same way from a different direction: its worker-count argument defaults to
more than one, so the command forks multiple children, each firing worker-startup
independently. The owned entrypoint (`src/worker_main.py`) starts the broker exactly
once, then runs the scheduler loop and the receiver as two supervised asyncio tasks,
with a signal handler that stops the scheduler first, drains the receiver within a grace
window, and shuts the broker down last. Two behaviors of the underlying library are
guarded explicitly because neither fails loudly on its own: the receiver's loop retries
with no backoff by default, so a broken Redis connection becomes a hot spin, and is
wrapped with backoff plus a completion callback that logs and exits rather than dying
silently; and cron last-run state lives only in memory, never persisted, so a naive
restart's first tick could fire spuriously against whatever the current minute happens
to be — the worker skips its first tick on every start to avoid this.

**Scheduling is by cron, never by interval.** An interval-based task is treated as due
whenever it has no recorded last run, so it fires on every process start regardless of
whether the first tick is skipped; cron is the only schedule shape that stays safe
across restarts. Each scheduled task's identifier is pinned explicitly in its schedule
label rather than left to the library, which mints a fresh one per process start —
useless as a correlation key in logs or as a dedupe key for anything downstream. The
schedule source used reads these labels at startup and creates no coordination keys of
its own, in preference to an alternative that adds a key family and issues a
multi-key read unsafe under the clustering policy in use.

**Broker choice.** The stream-based Redis broker is used rather than the alternatives
the library offers: a queue-based broker is rejected because its reconnect guard, per
its own source, catches the wrong connection-error type and is effectively dead code —
a known failure mode against Azure's managed Redis, where a blocking read can hang
indefinitely; a publish-subscribe broker is rejected outright because it broadcasts,
so a task that deletes resources would execute once per subscriber rather than once.
Several of the broker's arguments are set explicitly rather than left at their
defaults, each closing a specific silent failure: the stream and consumer-group names
are namespaced per environment, because the Redis instance is shared and the library
defaults both to the same bare name regardless of environment; the lock taken while
processing an unacknowledged message is given an explicit expiry, because it otherwise
never expires, and a process killed while holding it would wedge the key permanently;
the stream is capped to an approximate maximum length, because acknowledging a message
does not trim the stream, so an untrimmed stream carries no expiry and grows without
bound; and the connection pool's size is bounded explicitly, because the client
library's own default is effectively unbounded. The broker's connection timeout is also
set explicitly, longer than the interval its blocking read waits per poll — a shorter
timeout would turn a healthy idle worker into a reconnect loop, since a normal blocking
wait would then look identical to a dead connection.

**No result backend.** Nothing awaits a reconciliation result, and the broker library's
default result backend deserializes arbitrary objects from unprefixed keys in a Redis
database shared with other applications — a needless remote-code-execution surface for
a value nothing ever reads.

**No retry middleware; acknowledgment happens on receipt, not after execution.** For
work that deletes cloud resources, at-least-once delivery buys nothing — a lost pass is
simply re-driven by the next scheduled tick — and risks a second reconciler running
concurrently with the first. The broker has no delivery-count cap and no dead-letter
queue, so retrying a message that crashes the worker would otherwise be an unbounded,
destructive retry loop. Transient failures inside a pass are handled per item within
the pass itself, never by redelivering the whole message.

**Dependency layout.** The task decorators and the broker module belong to the core
dependency set, importable regardless of which role a process runs, so a task-decorated
module costs nothing to import even where it never executes. The concrete Redis broker
implementation lives in a separate group installed only for the worker role. Both
groups are part of the default install set, so a local run and the built image cannot
diverge, and the project's type checkers can resolve the broker module everywhere.

**Import discipline is a runtime property here, not a style preference.** Task modules
import only the broker, structured logging, and their own settings at module scope;
anything heavier is imported lazily inside the task body, after any feature-flag check,
so a disabled task costs nothing beyond that import. Broker construction is
unconditional rather than gated: when the Redis connection settings are absent — as in
tests — an in-memory broker is built instead, which keeps the import path total and
removes the need for a test-time monkeypatch that would not work anyway, since a
decorated task binds to its broker at decoration time, not at call time. Any lifecycle
handler on the broker is registered once, at module scope, never as a side effect of the
function that builds the broker — building it twice would otherwise register the
handler twice.

## Rejected alternatives

- **Keep scheduled work inline in the request-serving process.** Rejected on the
  properties in Context — most sharply, that it is unobservable, which is worse for work
  that deletes cloud resources than for any other kind.
- **A scheduled job on the platform's own timer-triggered container primitive**, instead
  of an always-on worker. Rejected for this workload: the reclamation pass holds a
  database-level advisory lock and must complete inside a bounded window, a fresh cold
  start on every scheduled invocation would be paid against an image sized for the whole
  backend, and a one-shot job gives no natural home for the next scheduled task.
- **A result backend.** Rejected for the reason given above — an arbitrary-object
  deserialization surface for results nothing reads.

## Consequences

- A second long-lived process exists alongside the API and has to be operated:
  provisioned, granted its own identity, and monitored for how long it has been since
  its last successful pass.
- Because the container platform drains the previous revision while a new one starts,
  two scheduler instances briefly coexist during every deploy — the library has no
  leader election. Every scheduled pass is therefore written to be idempotent and safe
  to run more than once concurrently, rather than relying on the replica count for
  exclusivity.
- Liveness is not a health-check endpoint for this process; at least one scheduled
  pass — reclamation — records a durable row for every run, including a declined or
  failed one, specifically so an operator can tell "a pass ran and found nothing to do"
  apart from "nothing has run in a long time."
- Adding the next scheduled job is now a small, well-worn change: a task module, a cron
  schedule label, and a feature flag — not a new place to put a loop.
- The orchestrator that builds and iterates on generated apps remains a separate,
  standalone service; it does not run as a task on this queue.

## Related

- ADR-0013 (the async session machinery the worker reuses for its own database access)
- ADR-0014 (the orchestrator stays a standalone service and does not move onto this
  queue)
- ADR-0015 (the worker's deployment as the backend image's second run target)
- ADR-0029 (the fleet reclamation pass that is this queue's most demanding passenger)
