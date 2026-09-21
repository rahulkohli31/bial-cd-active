# Reclamation escalation

What to do when the automatic reclamation pass reports a sandbox container it could not judge, or
when you suspect the pass itself has stopped running. Reach for this whenever a
`sandbox_reclamation_candidate` escalation shows up in the logs, when the fleet-size alarm fires,
or when nothing seems to be reclaiming anything and you want to know whether the worker is even
alive.

## What escalation means here

The reclamation pass never destroys a container it could not fully judge. Every unreadable
signal — a missing identity tag, an unreachable application database, a container stamped by
another control plane — routes to **escalate**, which means a human must decide. Nothing times
out into a deletion: a container can sit escalated indefinitely, billing, until somebody looks.

Escalation is not an error state. It is the pass declining to guess, and the queue only shrinks
when a person acts on it.

## Who is on the hook

| | |
|---|---|
| **Owner** | The platform operator role for this deployment — whoever holds superadmin access is positioned to run the levers in `reconcile-and-reclamation.md` and act on what they report. |
| **Channel** | Wherever this deployment already routes its structured operational logs. This runbook names the exact log events to watch below; wire those into whatever alerting the deployment already has, rather than adding a second, parallel channel. |
| **Response time** | No fixed deadline: nothing here times out into a deletion, so there is no data-loss clock to beat, and an escalated container's only cost is what it continues to bill. Triage it on the platform operator's normal cadence. The one time-sensitive exception is the dead-worker signal below — the absence of a pass record for three scheduled intervals means reclamation is not running at all, which is an outage on the whole fleet, not one container, and is worth treating as one. |

Everything below works without a formal SLA attached to it. Nothing below reaches anyone until the
channel above is actually wired up.

## The signals to watch

| Log event | Meaning | First action |
|---|---|---|
| `sandbox_reclamation_candidate` with `verdict=escalate` | one container could not be judged | read its `reason`; classify it with the triage table below |
| `sandbox_reclamation_store_fault` | the pass refused to judge the whole fleet — too little of it is accounted for by Redis | do not act on any verdict from that pass; check Redis first |
| `sandbox_fleet_over_threshold` | the fleet is larger than anyone intended | not an error by itself; a cost signal worth a look |
| absence of a pass record for three scheduled intervals | the scheduled worker is not running | the only detector of a dead worker — see below |

That last row is the one to internalise. Every alarm above is emitted by the pass itself, so a
crash-looping worker emits none of them and reads exactly like a healthy, quiet fleet. The absence
of a pass record is the alarm. `reconcile-sandboxes`, in `reconcile-and-reclamation.md`, returns
the last-pass timestamp and a staleness flag for exactly this reason.

## Triage

Run `reclamation-report` first (see `reconcile-and-reclamation.md`). It runs the same classifier
against the live fleet, destroys nothing, and returns every verdict with its tier and its reason —
which is what lets you disagree with a decision rather than only accept it.

| Reason | What it means | Resolution |
|---|---|---|
| "carries no identity this control plane can judge" | the container has no identity tags, or they were stamped by a different environment | run `backfill-sandbox-tags`; anything still unowned afterwards has no matching application record and must be deleted by hand |
| "the product database was unreadable" | the application database could not be reached | fix the database; the next pass re-judges the whole fleet automatically |
| store fault on the whole pass | Redis is missing, flushed, or pointed at the wrong instance | fix Redis. Do not delete anything on the strength of a store-fault pass: every container reads as unclaimed |

An escalated container that has since been resolved needs no action to un-escalate — the next pass
re-judges it from scratch.

## Deleting by hand

The platform will not do this for you, deliberately. When triage says a container is genuinely
abandoned:

1. Confirm the name is not one a live builder is using — check the response of
   `reclamation-report`, not your memory.
2. Delete the Container Apps instance directly, by name — for example:
   `az containerapp delete -n <container name> -g <resource group> --yes`.
3. Nothing else is required. The registry record, if any, is cleared by the next sweep.

## Exit condition for report-only mode

The destroy flag should not be armed until three consecutive passes have produced a candidate list
an operator agrees with, and the tag backfill reports zero untagged sandboxes. Three, not one: a
single agreeable pass is one reading, and the two-pass staging protocol means the first pass of a
newly-enabled deployment has nothing staged yet to compare against.

Both the enable and destroy flags for the reclamation pass default off (see
`backend/src/services/sandbox/config.py`). The pre-existing abandoned-sandbox sweep is a different
thing entirely: it defaults on, reaps containers the registry already knows are stale, and has
been running since long before the reclamation pass existed. Turning the reclamation flags on does
not change whether that sweep runs.

## Where to go next

- `reconcile-and-reclamation.md` — the levers named throughout this runbook.
