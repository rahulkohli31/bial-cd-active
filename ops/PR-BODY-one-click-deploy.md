A citizen answers six data-classification questions, presses Deploy, and their app goes
live. No admin approval on this path; the existing submit/approve/reject/disable surface is
untouched and simply not what this route calls.

```
POST /v1/projects/{id}/deploy      → 202 {deploymentId}          (~95ms measured)
GET  /v1/projects/{id}/deployment  → poll to succeeded / failed
```

## The gate is inside the deploying request

The answers ride in the deploy body and are scored server-side, in the same request that
publishes. There is deliberately **no separate "score my answers" endpoint**: one that
merely reported a number would be advisory, and any caller that skipped it would reach the
pipeline unscored. Clearing the gate and being deployed are the same event.

At or above **50** the deploy proceeds automatically. Below it the request is refused on
**409 `classification_below_threshold`** with the score, the threshold, and the categories
that were not declared — and *nothing else happens*: no row is claimed, no workspace is
saved. The gate runs before `_resolve_unsaved_work`, which writes, so a refused deploy
cannot leave a side effect behind. Not 403: `chatErrors.ts` reads a 403 on this surface as
"your session lapsed", so a refusal sent on 403 would reach the citizen as a login problem.

A second threshold at **25** makes the explanation box mandatory, and the two are
independent on purpose — PII + Financial (40) must explain itself *and* is still refused. An
unexplained sensitive declaration is a **422**, not a refusal: it is an incomplete
submission, and telling someone whose answers actually qualify that they failed the gate
would send them back to change answers that were correct.

The weights and both thresholds live together in `services/deploy/classification.py`. They
are one policy unit — a threshold in configuration and weights in code can drift into a
combination nobody chose. `answers` is a **required** field, which is what makes this a gate
rather than a prompt: there is no shape of the request that deploys without a declaration.

What was declared, and the score that authorised it, land on the `deployments` row in the
same INSERT that claims the slot — a second write would leave a window where a crash
produces a running deploy with no record of what allowed it. Per deploy, not per app: the
agent edits the app between deploys, so a declaration attached to `app_registry` would keep
describing a version that is no longer running. Stored, never recomputed — the weights are
policy, and recomputing later would report what *today's* table says about an *old*
declaration.

The questionnaire, the weights, and the notes threshold come from #111.

Between them, detached: save-if-dirty → extract the saved bundle server-side → pack a build
context with the platform's own Dockerfile → build the image in ACR → provision a
digest-pinned Azure Container App at `minReplicas: 0` → wait for the revision → record it,
audit it, and tell the citizen in chat.

The app keeps the **same** per-project database and Blob container it had in the sandbox, and
the URL is stable across every redeploy because it derives from the immutable app id.

## This has been run for real

A generated app is live in Azure, deployed by this pipeline. Playwright drove a real browser
end to end — opened it, clicked through to the form, filled it, submitted — and the record
came back **after a full page reload**, read from the project's own Azure PostgreSQL by a
fresh server render. Browser → server action → database → re-render.

Proven against real Azure: the build context (48 files, real commit SHA), the platform
Dockerfile (96 MB, non-root), standalone tracing pulling in `drizzle-orm/node-postgres/migrator`,
the `public/.gitkeep` placeholder (the golden template ships no `public/`), the strict
migrator applying three migrations — and refusing to start the server when the database was
unreachable, which is exactly its job — ACA provisioning, revision health, and the injected
DSN and Blob SAS as ACA secrets.

**One call is unproven:** `scheduleRun` returns `TasksOperationsNotAllowed` on the free-trial
dev subscription (as `sandbox/Dockerfile.sandbox` already records). The image was built with
`docker buildx` and pushed — the same route the existing per-app images in that registry
took — and everything downstream ran for real. BIAL's registry is the first place that call
can run; see `ops/ONE-CLICK-DEPLOY-PROD.md`.

## Two bugs the live run found that 145 unit tests did not

**Every build would have failed.** The `next.config` wrapper carried an `@ts-expect-error` on
its import of the app's renamed config. TypeScript resolves that file perfectly well, so
there was nothing to suppress — and an unused expect-error is itself an error.

**Every deploy would have timed out.** `get_revision` read ARM's state with `str()`. The SDK
returns enum members, so that yields `RevisionProvisioningState.PROVISIONED`, not
`Provisioned` — a healthy revision reported unhealthy, burning the whole readiness budget
before failing a deploy that had already succeeded.

Both were invisible because the fakes hand-wrote tidy strings. The fakes now build their
state through the same normalizer the real client uses. Same lesson as the ARM-poller commit.

## Load-bearing decisions

**The pipeline never provisions or restores a sandbox.** `restore_from_snapshot` tears the
container down *before* it pulls the bundle, and a confirmed-absent snapshot falls through to
a blank golden template — which would build cleanly, deploy successfully, and replace the
citizen's app with the starter under a green checkmark. The bundle is read from object
storage instead.

**The agent cannot hijack its own build.** Four independent layers: its Dockerfile is
excluded when the context is packed rather than overwritten; the assets come from the backend
image; the Dockerfile path is named explicitly in the build request; and the build runs
`npm ci --ignore-scripts` and `npx next build` rather than `npm run build`, because
`package.json` is agent-editable.

**One in-flight deploy per app, enforced in Postgres** by a partial unique index — the repo's
first. The pipeline runs for minutes and the control plane restarts on every platform deploy;
an in-process guard goes blind across exactly that restart. A crashed pipeline's row is
recoverable, or a citizen's Deploy button 409s for half an hour with nothing to explain it.

**The reconciler may promote, never delete.** A matching image digest promotes a row the
pipeline died before writing; a different digest fails the row and leaves the container alone
(it is the citizen's previous, working version); an unreachable ARM leaves the row untouched
rather than guessing. That last arm matters most — collapsing "throttled" into "gone" would
eventually mark a live app failed. The read-only protocol makes it a type guarantee.

## Also in here

`fix(sandbox)` is the only commit touching the existing sandbox path: `poller.result()` had
no timeout and runs on a six-thread shared executor, so a few wedged ARM calls would stall
every `asyncio.to_thread` in the process — the reaper's deletes, snapshot extraction,
offloaded storage.

`style(test)` fixes a ruff failure that is **pre-existing on `release/1.5.0`** and unrelated
to this work, included only so the lint gate is green.

## Not in here

**Authentication on published apps.** The ACA environment is internal-only, so they are not
on the public internet — but any member of staff with the URL can open any deployed app and
read or write its data. Separate task, and stated in the router docstring so it cannot be
discovered by surprise.

Also deferred: the portal Deploy button (backend only), blue/green traffic splitting, custom
domains, ACR retention, and rollback beyond ACA keeping the previous revision serving.

## Before this can run in production

`ops/ONE-CLICK-DEPLOY-PROD.md`, with a paste-ready role definition. In short: a five-action
custom role on `bialgenaicr` (**not** `AcrPush` — the registry's own agent pushes), the
`DEPLOY__*` app settings, and two 15-minute pre-flight checks that can each silently sink a
deploy.

## Verification

All four gates clean across 426 files — ruff, ty, mypy `--strict`, pyright. 222 tests over
the changed areas.

Note `release/1.5.0` currently has **four failing tests unrelated to this branch** (admin
roster, admin usage aggregate, the Entra callback error path, and the app-database wall
self-heal). Confirmed by running them on the base commit before this branch existed.
