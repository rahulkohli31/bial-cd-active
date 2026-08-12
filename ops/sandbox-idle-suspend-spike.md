# Idle-suspend for sandboxes — spike before build

**Status:** not started. This is the design and the verification list, not a decision.
**Why it is a spike and not a PR:** the substrate it depends on is in public preview and several
load-bearing facts could not be confirmed from Microsoft's own documentation. Building against
them now would be guessing with a migration.

---

## The problem

A sandbox is pinned `minReplicas = maxReplicas = 1` and cannot scale to zero, because the user
must be able to come back to a warm app. So every sandbox bills 24/7 whether anyone is looking at
it: 1 vCPU + 2 GiB, roughly **$78/month each** at ACA consumption rates.

That is also *why* the slot is one-per-user. The count only matters because the containers never
sleep — which is the actual thing to fix. Every comparable product (Lovable, v0, Replit,
CodeSandbox) suspends on idle and resumes on demand, and none of them enforces a limit as tight
as one.

**Suspend on idle and the slot limit stops being felt.** A user could have five projects with only
the one they are looking at warm. That is the goal; the number is not.

## What NOT to do

**Do not raise the per-user slot count as a shortcut.** It is not a config change and it would not
fix anything users hit today:

- `bial:sandbox:lock:{user_id}` is a `SET NX` **string** — binary, held or not. Two slots needs a
  semaphore.
- `bial:sandbox:registry:{user_id}` is **one hash per user**, structurally one app.
- `_active_by_user` is `dict[UUID, UUID]` with 13+ single-valued call sites.
- `reap_user` / `reconcile_user` / `sweep_all` all assume one sandbox per user.

That is a coordination-layer rewrite of the most data-loss-sensitive code in the system, and with
#83 fixed a switch is already safe. Raising N to 2 would only move the same eviction to the third
project.

**Rule out ACA dynamic sessions.** Researched and rejected: no persistent storage, and WebSocket
support is undocumented — so no HMR, which the whole preview depends on.

## The candidate: Azure Container Apps Sandboxes (public preview)

On paper it is an exact fit — microVM isolation, memory+disk snapshot, no CPU/memory charge while
stopped, Entra-only access, and a default M tier of 1 core / 2 GB matching current sizing. If it
delivers, it collapses this phase and most of the autosave work into a platform primitive.

### Verify before designing anything (in order)

| # | Question | How | Blocks |
|---|---|---|---|
| 1 | Is it GA, or still preview, in **centralindia**? | `az containerapp sandbox --help`; region availability list | everything |
| 2 | **Measured** resume latency from a real snapshot | provision one, suspend, resume, time it — 10 runs | the whole UX premise |
| 3 | Does the auto-suspend default actually exist, and what is it? | Reported as 300s **in a GitHub CLI skill file only**, not the docs | reclaim timer design |
| 4 | Are there really only two lifecycle states? | Canonical docs list Running/Stopped; a four-state model with auto-resuming *Idle* appears **only in an early-access doc** | a wake-on-preview-request design hangs on this |
| 5 | Exact port-auth CLI syntax for Entra-gated ingress | Microsoft warns the CLI/SDK surface may change during preview | preview URL delivery |
| 6 | Does a suspended sandbox survive a `next dev` process + open HMR socket? | suspend mid-session, resume, check the socket reconnects | whether the preview survives at all |
| 7 | Cost while suspended, measured not quoted | run one for a week suspended, read the bill | the entire business case |

**Do not design around 3, 4 or 5 until they are confirmed from first-party docs or your own
measurement.** They are the three the research explicitly could not stand behind.

### Decision criteria

Adopt if: resume is **under ~3s** measured, suspended cost is **near zero**, and the dev server
survives a suspend/resume cycle. Otherwise fall back below.

## Fallback that needs no preview features

If ACA Sandboxes does not qualify, idle-suspend is still buildable with what exists today, because
the pieces are already in place:

1. `write_snapshot(..., recovery=True)` already captures a tree without touching the user's saved
   bundle.
2. The reaper already has the idle signal (`heartbeat_is_alive`, `stay_of_execution_is_current`).
3. `_restore_or_provision` already rebuilds a container from a bundle.

So: on idle expiry, snapshot to recovery → `delete_app` → clear the registry. On return, restore
from the newer of recovery/snapshot. It is a **cold** resume (image pull + restore, tens of
seconds) rather than a warm one, so it needs honest UI — *"Reopening your project…"* — and it is
strictly better than today, where the same delay happens anyway and the work is lost.

The cheap intermediate, if this is deferred: keep the container but **shorten the stay of
execution**, so an abandoned sandbox is reclaimed in minutes rather than never. Most of the money,
none of the new machinery. That is the one to do first if nothing else here gets funded.

## Prerequisite regardless of substrate

The reaper's live-session shield reads an **in-process set**, so the single-replica constraint is
binding (`reaper.py` says so itself, and `main.py`'s sweeper inherits it). Any suspend/resume
scheme that runs on a timer will eventually want a second replica. Replacing that shield with a
Redis-backed lease is the real unlock — and once liveness is shared rather than in-process,
raising the slot count from 1 to 2–3 becomes nearly free, if it is even still wanted.

## What already shipped, so this phase is not urgent

- A project switch no longer destroys unsaved work (#83).
- A reclaimed preview no longer pretends to be live (#83).
- Containers nothing is tracking are now reportable (`POST /v1/admin/reconcile-sandboxes`) —
  the twelve-day orphan class.
- Every Write turn autosaves to a recovery slot.

The remaining cost of *not* doing this phase is money, not correctness.
