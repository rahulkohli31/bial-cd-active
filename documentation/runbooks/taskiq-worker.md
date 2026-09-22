# Taskiq worker runbook

The Taskiq worker is the platform's background process. It runs the recurring reconciliation and
cleanup passes the control plane does not run inline — settling stalled deployments, sweeping
abandoned sandboxes, evaluating whether any container has drifted from the platform's own records,
and removing conversations nobody has come back to. Reach for this document to start the worker,
confirm it is actually alive, or size and troubleshoot it.

## 1. It will not boot from the control plane's own environment file — check this first

The worker and the API are two different roles built from the same codebase, and each declares
its own, minimal configuration. The worker authenticates no request, serves no browser, calls no
AI model, and never opens a per-project database connection — so its configuration type does not
even declare fields for any of that. Configuration loading additionally refuses any field it does
not recognize, rather than ignoring it.

The practical effect: **point the worker at the same environment file the API uses, and it will
not start.** It fails immediately, before doing anything else, with a validation error listing
every field the API needs that the worker does not — an authentication secret, a per-project
database credential, and so on. That is deliberate: a worker holding a credential it has no use
for is a worker holding a credential for no reason.

The fix is a dedicated, worker-only environment file. A template for exactly that is tracked at
`backend/.env.worker.example`, and its own header comments explain how to point the process at a
copy of it. Do not hand-carve one from the API's file by trial and error — the template already
lists precisely what the worker needs and nothing else, and a dedicated test keeps it honest
against the worker's actual configuration.

## 2. Running it

Locally: run the worker's entry point with the project's managed Python environment — never a
bare system Python interpreter, using the worker-only environment file from step 1. Healthy
startup logs two lines: confirmation that each background task module registered, followed by a
line confirming the process is running both the task receiver and the scheduler together, in one
process.

In production, the worker runs as its own container, from the same image as the API, with its
start command and role overridden. Two details in that override are load-bearing and have each
caused a real startup failure:

- **Point the command at the interpreter inside the image's managed virtual environment, not at
  a bare `python`.** The image sets no general command path into that environment — the API's own
  container avoids needing one by always going through the package manager's own run wrapper — so
  a bare `python -m ...` resolves to a system interpreter with none of the dependencies installed,
  and dies on the first import, before it has read a single setting. That failure names none of
  the things an operator would then go check.
- **Pass the module invocation as one unbroken token, with no space after the module flag.** The
  tooling available for setting a container's command and arguments cannot reliably carry several
  separate argument tokens — some silently join everything into one string, some choke on any
  argument that looks like a flag. A single joined token survives every path that sets it.

## 3. Verifying it is alive

The worker exposes no health probe, deliberately — it has no HTTP listener at all, so there is
nothing to poll. Within a few minutes of a fresh start you should see a log line for each
scheduled task's first tick. A task that is present but not configured for this environment logs
that plainly as disabled — a distinct, expected outcome that must not be read as the same thing as
a failure to reach its target.

Two of the worker's scheduled passes record a durable outcome row in the product database on every
tick — success, decline, or failure alike: the fleet-reclamation pass (section 4) and the
conversation-retention pass (section 5). That is what makes their staleness a reliable, queryable
signal that the whole worker process has stopped, independent of whether anyone was watching logs
at the time. Lean on reclamation for liveness rather than retention: reclamation runs on a short
cycle, while retention declines most of its own ticks by design and says so in the record it
writes. The other scheduled passes — deployment reconciliation and the routine sandbox sweep —
only log their outcome; confirm those are still running from their own log lines, since neither
leaves a queryable record behind.

## 4. The reclamation pass is report-only by default, everywhere

One of the worker's scheduled passes compares the live container fleet against the platform's own
records and, in principle, can delete containers it judges orphaned. As shipped, it is gated by
two independent settings: one that turns the pass on at all — so it enumerates and reports what
it sees — and a second, separate one that lets it actually delete anything. **Both default to off,
in every environment**, and that split is deliberate: deleting the wrong container is exactly the
mistake the second gate exists to prevent.

This is the pass whose durable record section 3 leans on for worker liveness, and it is also the
worker's only path to deleting a container. For how to preview what it would do, how to escalate a
container it cannot judge, and when it is safe to arm the destructive setting, see
`reconcile-and-reclamation.md` and `reclamation-escalation.md` — this document covers only the
worker process that runs it.

## 5. The conversation-retention pass ships switched off

This pass removes any conversation — a Plan chat, a Build chat or a BIAL Chat alike — that nobody
has added to for longer than the retention window, along with its messages, its attachment records
and the files behind them. It is gated by a single setting that the worker **requires**: a
deployment that does not carry it refuses to start rather than guessing, and the template in
`backend/.env.worker.example` ships it set to off.

**Read this before switching it on.** Nothing has ever deleted a conversation on this platform, so
the first enabled pass is not a week's worth of tidying — its candidate set is the entire history,
which is every conversation of every kind whose newest message is older than the window, including
Plan and Build conversations that have existed since launch. That is the intended behaviour, and it
is irreversible.

Three properties make it safe to leave running once it is on. It removes a bounded number of
conversations per run, recording how many candidates it left behind, so the first pass over the
historical backlog drains across several runs rather than attempting one transaction that a deploy
would roll back. It re-checks idleness at the moment of deletion, so a conversation somebody
returns to between selection and deletion is spared. And it takes a database advisory lock, so the
two scheduler instances that briefly coexist across a deploy cannot both run it.

The window and the per-run ceiling are worker settings too, both with working defaults; the
template from step 1 names all three and says what each one means. Reading this pass's own history
is the same query as for reclamation — the outcome rows in section 3, under its own task name.

## 6. Redis provisioning gates

Confirm three things about the Redis instance directly with whoever provisions it, and record the
answers — the worker's own logs cannot tell you any of this on their own:

1. Its clustering and eviction posture must not produce "the worker consumes nothing" as a
   side effect of an unrelated setting — that symptom is otherwise indistinguishable from a
   worker that simply has no work.
2. Its key-eviction policy must not evict the platform's non-expiring coordination keys ahead of
   the ones that carry a deliberate expiry — under the wrong policy, exactly the keys meant to
   persist are the first to go, which is backwards from what the platform assumes.
3. Its data-persistence setting should be recorded, not assumed.

Confirm which Redis product and connection port the target subscription actually provisions
before go-live rather than assuming a value from an earlier environment or an older copy of this
document — managed Redis offerings and their defaults change over time.

## 7. Alerting on a dead worker

The reclamation pass's own staleness (sections 3–4) is the primitive to alert on. A fleet-size
alert alone is not sufficient: it is emitted *by the pass itself*, so it goes silent at exactly
the moment the pass dies, and a dead worker then reads identically to a healthy, quiet fleet.
`reclamation-escalation.md` names the exact signals to wire into an alert, and what to do once one
fires — start there before relying on this in production.

## 8. Sizing

Run the worker as a single replica. Correctness does not rest on that number — two schedulers
briefly coexist on every deploy, and the passes are built to tolerate it: each is idempotent, and
the reclamation and retention passes each additionally take a database advisory lock, on separate
keys, so only one instance of either runs at a time. What a second replica costs is duplicated
work and doubled load, not a corrupted fleet. A container platform's scale-to-zero behavior is
equally wrong here for the opposite reason — the worker has no inbound traffic to scale back up
on, so once it scales to zero it never restarts itself. After the worker has run for a week in a
given environment, check its memory headroom and resize if it is running close to its limit.
Under-provisioning memory here is recoverable rather than dangerous: a crash mid-pass leaves the
fleet in a state the next pass reconciles cleanly.

## 9. What this process may never do

No code in the worker may assert that a build session is certainly, unrecoverably dead and act on
that alone — that assertion is only ever true if the platform is certain it is the only replica
running, and a background process can never establish that about itself. A worker that acted on
it anyway would, during the brief window where two replicas run side by side across a deployment,
reap a session that is very much alive out from under its owner. This boundary is enforced by an
automated check that scans every file in the worker's own code, recursively, for that exact
assertion — not by a review convention — because a worker task once organized as its own
sub-package evaded an earlier, non-recursive version of the same check.

## Where to go next

- `reconcile-and-reclamation.md` — the levers for previewing, escalating and running the worker's
  passes on demand.
- `reclamation-escalation.md` — what to do when a container cannot be judged, or the worker itself
  goes quiet.
- `redis.md` — the coordination store this process shares with the API.
