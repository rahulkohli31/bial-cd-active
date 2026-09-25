# ADR-0030: Withdraw Tiered Reclamation, Keep What the Sweep Already Did

## Context

ADR-0029 designed a destructive pass with its own authority: enumerate Azure as the fleet of
record, stage a candidate for one interval before acting on it, and destroy by confidence tier
once identity tags covered the whole fleet. It shipped with destruction off everywhere and a
written exit condition — three consecutive report-only passes a named human agreed with, plus the
tag backfill reporting zero untagged containers — before it could ever act.

Neither precondition was ever met. The backfill never ran to completion against a fully-tagged
fleet, so the pass's own report-only preview was never something a reviewer could trust the way
the exit condition required, and nobody signed off three passes of it. The destroy tiers were
therefore unreachable by construction in every environment this platform runs, for the whole of
the pass's life. What actually did the fleet's deleting the entire time was the sweep ADR-0029's
own Context describes as predating it — the unflagged, always-on reconciler that walks the
coordination store's registered users and reaps what has lapsed. Roughly 1,350 lines of source and
2,700 of tests stood behind a report a human never got to sign off on, over a feature no container
was ever staged or destroyed by.

## Decision

**The destroy pass, its staging marker, its confidence tiers, and its own report endpoint are
removed.** `reclaim.py` (the classifier), `reclamation_pass.py` (the pass), the worker task, and
the `/reclamation-report` preview all go, along with the two settings that gated them and the ARM
tag that marked a two-pass staging candidate. Nothing that ran today changes behaviour: the flags
were off, the tag was never written outside a test, and the population reclamation was built to
collect is still found the way it always has been — an operator running the orphan inventory by
hand and deleting what it names.

**Everything the destroy pass shared with the sweep that was already destroying stays, unchanged.**
`reap_user` and `sweep_all` keep the four-step destroy ordering (mark the registry entry ending,
tear the resource down, delete the registry entry, release the lock last), the durable-copy gate
before anything is destroyed, and the wall-clock liveness lease that tells a scheduled pass a build
is still in flight. A workspace whose repository is gone — which no write-back could ever save
regardless of how many passes examined it — is reclaimed rather than spared forever, on the same
principle: nothing is lost that a later pass could have kept. Identity is still stamped on every
container at creation and by the backfill for what predates it, because the sweep's own age
ceiling reads that stamp off ARM when the coordination-store record cannot be trusted. The
absolute two-hour ceiling on an open tab still applies with no off switch. None of this depended on
the destroy pass; the destroy pass depended on it, and its removal changes nothing about how any
of it runs.

**The destroy pass's own predicates over the identity tags go with it; the tags stay.**
`SandboxIdentity.is_a_sandbox`, `.escalate_only`, `.was_backfilled` and `FleetMember.identity`
judged a container for the pass and have no other reader. The tag contract they read is unchanged:
every container is still stamped with its kind, owner, app, control plane and age, and the sweep
still parses those tags.

## Consequences

- A fleet-enumeration log line is gone: the 15-minute pass named the subscription, resource group
  and managed environment it was judging every time it ran, and nothing else in this codebase logs
  that. An operator debugging which fleet a deployment is pointed at now has one fewer source for
  it.
- The worker process's only in-product liveness signal is gone with it. The same pass wrote a
  durable row every 15 minutes, `declined` or not, and that row's staleness was the one thing
  surfaced on the admin fleet report that could distinguish a crashed worker from an idle one.
  What is left — a durable-copy row, written only when a reap actually takes one, and (after
  ADR-0011's newest passenger) a daily conversation-retention row — each proves its own pass ran
  and nothing about the worker process as a whole.
- A forgotten container — one whose coordination-store record is gone — is found and removed the
  way it always has been in practice: `POST /v1/admin/apps/reconcile-sandboxes` names it, an
  operator deletes it by hand. ADR-0029 was building toward closing that by cadence instead; this
  withdraws the attempt rather than replacing it.

## Rejected alternatives

- **Leave it in place, switched off, as documented but dormant machinery.** Rejected on this
  codebase's own no-dead-code standard: a subsystem nothing calls and no environment enables is not
  a safety margin, it is surface nobody is verifying against a real fleet, and it was already large
  enough that three independent passes over it disagreed about what was actually still reachable.
- **Have the sweep write the same liveness row the destroy pass did**, to keep an in-product
  dead-worker detector. Rejected as new behaviour riding along on a removal: it is a real gap (see
  Consequences), but closing it is a decision about what the worker's liveness contract should be,
  not a side effect of deleting a pass that never ran.
- **Move the fleet-identity log line into the sweep.** Same reasoning — worth having, not worth
  smuggling into this change.

## Accepted risks

The worker process as a whole has no in-product liveness signal after this change. Its two
remaining scheduled passes each prove themselves; nothing proves the process is alive when both
happen to find nothing to do. Accepted because building that signal honestly is a decision with its
own design questions — which pass's cadence it should track, what "stale" means for a process with
more than one passenger — not a byproduct of a deletion.

## Related

ADR-0029, most of which this withdraws; the identity tags, the durable-copy gate, the four-step
destroy ordering, the liveness lease and the absolute ceiling it also describes remain in force
exactly as written there. ADR-0011 (the queue this pass ran on, and the passenger list it named).
ADR-0014 (the sandbox environment the sweep still holds destroy authority over).
