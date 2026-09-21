# Approved-application go-live runbook

This is the manual handoff between an approval decision and a running container. An operator
approved a submission; this procedure is how a human ships it. Building the image, creating the
running container, and injecting its runtime configuration are all done by hand here — none of it
is automated for the applications this procedure covers.

**This procedure applies only to applications on the platform's legacy manual-deployment
lineage.** Every application approved through the platform's own publish flow deploys itself, and
does not go through this document at all — the platform actively refuses to record a manual
deployment against one of those. The admin queue only shows this procedure's affordances (a
"Deploy needed" indicator, a "Mark deployed" action) on rows this procedure applies to; if you do
not see them for the row you are looking at, stop and confirm the application's lineage before
doing anything else. `reference/openapi.json` carries the exact field the admin API uses for this.

**Who runs this.** One person holds both hats by design, with no separation of duties at this
gate: the operator with the platform's superadmin role who approved the application is also the
one with the cloud access to build its image and create its container. Downloading the bundle,
pulling runtime credentials, and recording the handoff are done through the admin API or console;
building the image and creating the container are done directly against the cloud subscription.

## 0. Inputs

Open the admin app registry's approved queue. Each row (and its API projection) carries
everything this procedure consumes: the application's identity, its owner, the submission under
review, the **pinned, approved submission** (the one to actually deploy), and whether the current
pin has already been deployed.

> **The row's "login required" flag is recorded, not enforced.** Nothing reads it, and a deployed
> application authenticates nobody — anyone who can reach its address can open it. Only network
> posture stands in front of a published application, so treat that flag as a note about intent
> and decide reachability deliberately. `architecture.md` states the same gap.

> **Deploy the pin, not "the latest."** The bundle-download endpoint mints a URL for the
> application's *source* submission — the one currently under review. Immediately after an
> approval those are the same submission, but an owner can re-submit at any moment, which moves
> the source pointer to an unreviewed bundle while the row still says `approved`. The two checks
> below (steps 1 and 3) make shipping the wrong bundle by accident impossible. If at any point the
> row's status is not `approved`, stop — re-review is required before you continue.

## 1. Verify the row is deployable

Before doing anything else, confirm all of the following against the row:

- Its status is `approved` — a pending or rejected row is not deployable.
- It still needs deploying — a row whose current pin is already live has nothing to do.
- The submission under review is the same one that was approved — if not, an unreviewed
  re-submission has landed since approval, and it must not ship.

## 2. Download the pinned bundle

From the row's review view, download the bundle, or mint the URL directly through the admin API.
The URL is a short-lived, bundle-scoped bearer credential — use it immediately, and never paste it
into chat, a ticket, or a log. Minting it is audited: an operator pulling an owner's full source is
a gated action, and every mint is recorded with the submission id and its commit.

## 3. Reconstitute the tree, and verify the commit

Clone the downloaded bundle and read its `HEAD` commit.

> **The identity check.** That commit must equal the row's approved commit exactly. This is the
> procedure's own end-to-end proof that the bytes you are holding are the artifact that was
> reviewed — it closes the re-submission race even if you skipped step 1. On a mismatch, stop and
> re-review.

Install dependencies fresh — the checked-out tree never carries `node_modules` — and build the
application before spending any further time on an image. A build that fails here is a build that
must not reach a container.

## 4. Build and ship the container image

The generated application is a Next.js application, on the same Node.js baseline the build
sandbox runs. Applications on this lineage do not ship a Dockerfile in their own bundle, so the
canonical Dockerfile below is what every application on this path is built with — copy it into
the cloned tree verbatim rather than writing a per-application variant.

> This is **not** the sandbox's own image. The sandbox is a live development harness with its own
> control surface and must never be shipped to production. The image below runs the already-built
> application and nothing else.

<details><summary><strong>Canonical <code>Dockerfile</code></strong> (copy verbatim)</summary>

```dockerfile
# syntax=docker/dockerfile:1
# Generated-application production runtime image, for the manual go-live lineage only.
# One file, every application on this lineage — do not hand-author a per-application variant.
#   docker buildx build --platform linux/amd64 -t <registry-login-server>/<app>:<tag> --push .
# Secrets (BIAL_DATABASE_URL, BIAL_BLOB_SAS, BIAL_BLOB_CONTAINER_URL) are injected at RUNTIME
# as platform-managed secrets; BIAL_APP_ID / BIAL_PORTAL_ORIGIN as plain environment variables.
# NONE are referenced here.
ARG NODE_IMAGE=node:24-trixie-slim@sha256:0711b541c1c33a8a530ac4f0d391baa9a15b3d804695b1b24a47daa5fb60e74d

# --- deps: full tree incl. devDeps (the build needs the framework's own toolchain) ---
FROM ${NODE_IMAGE} AS deps
WORKDIR /app
RUN apt-get update \
  && apt-get install -y --no-install-recommends build-essential python3 ca-certificates \
  && rm -rf /var/lib/apt/lists/*
COPY package.json package-lock.json* ./
# A reproducible install only succeeds when the lockfile is in sync with package.json, and a
# generated application's lockfile drifts routinely — fall back to a plain install rather than
# refuse to publish outright.
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

# --- build: compile (a type error fails the image, by design) ---
FROM ${NODE_IMAGE} AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# --- runner: full built tree + full node_modules ---
# -slim lacks system libraries some native modules need at runtime. An application that builds
# clean but crashes at start is the symptom; swap to the non-slim base or install the missing
# library if that happens.
FROM ${NODE_IMAGE} AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build --chown=node:node /app ./
USER node
EXPOSE 3000
# Plain start, honoring $PORT and binding every interface. No migrate-on-boot: go-live on this
# lineage reuses the same database the sandbox already migrated. Opt in only if ever needed:
#   CMD ["sh","-c","node scripts/db-migrate.mjs; exec node_modules/.bin/next start"]
CMD ["node_modules/.bin/next", "start"]
```

</details>

<details><summary><strong>Canonical <code>.dockerignore</code></strong> (does not ignore <code>*.md</code> — some applications import Markdown as content)</summary>

```gitignore
node_modules
.next
out
build
.git
.env
.env.*
!.env.example
*.bundle
*.log
.DS_Store
Dockerfile
.dockerignore
```

</details>

Build for the `linux/amd64` platform and push to the registry the platform's container hosting
draws from:

```sh
docker buildx build --platform linux/amd64 \
  -t <registry-login-server>/citizen-app-<appId>:<approved-commit-short-sha> \
  --push .
```

Confirm before you build whether the target subscription can build the image for you, or whether
you need to build locally and push — do not assume either without checking. Keep line endings LF
and every path in anything you script here OS-agnostic: the platform's own image-build host is not
guaranteed to be the same operating system you are working on. Tag the image with the approved
commit's short SHA so the running revision is traceable back to the audit trail; this is the
convention every deployment on this lineage has actually used.

## 5. Run it, and inject its runtime configuration

### 5.1 — Pull the runtime credentials

Both credential endpoints are restricted to the platform's superadmin role and audited, and —
unlike downloading the bundle and recording the deployment — have no button in the admin console.
Call them directly, with an authenticated session, rather than through an interactive API
explorer: that surface is disabled in production.

- **The database credential** reveals the application's per-project database connection string —
  byte for byte the same database the sandbox has been using. Go-live neither creates nor seeds a
  database; the application's own migrations, already in the bundle, are what shaped it. A later
  rebuild session in the sandbox writes to this same live database, so treat that as a fact about
  the system, not a surprise. A missing application returns not-found; a project with no ready
  database returns a conflict response — never a server error.
- **The blob credential** — needed only if the application uses blob storage — mints the
  application's own long-lived, container-scoped credential. It reaches its own storage container
  directly, with no platform proxy in the data path and no dependency on the control plane being
  up. It requires the storage account to be configured with an account key rather than a managed
  identity; a managed-identity configuration cannot issue a credential this long-lived and the
  endpoint reports that plainly. The credential is shown once, is never logged and never appears
  in the audit trail — copy it straight into the container's secret configuration; if it is lost,
  mint a new one.

### 5.2 — Create or update the container, and inject its environment

Mirror the sandbox's own registry-authentication approach for the platform's container registry.
Inject every value at runtime — never bake one into the image. The database connection string and
the blob credential are secrets: inject them as managed secrets, never as plain environment
values.

| Variable | Kind | Source | Required |
|---|---|---|---|
| `BIAL_APP_ID` | plain | the row's application id | yes |
| `BIAL_PORTAL_ORIGIN` | plain | the platform's own public address | yes |
| `BIAL_DATABASE_URL` | secret | the database-credential response | yes |
| `BIAL_BLOB_CONTAINER_URL` | secret | the blob-credential response | only if the application uses blob storage |
| `BIAL_BLOB_SAS` | secret | the blob-credential response | only if the application uses blob storage |

`BIAL_PORTAL_ORIGIN` is easy to miss, because there is no endpoint to fetch it from — it is simply
the platform's own public address. Leave it unset and the deployed application's parent-frame
integration fails closed inside the platform's own frame.

Verify afterward: the application serves at its own address, and its data reads and writes hit
its own database directly, never the control plane. The disable action (from the admin console)
severs the application's database role — see the note on revocation below for exactly what it
does and does not cover. The container environment (and any firewall or private-networking rules
in front of the database) must let the application reach the database server directly, or its
data layer is dead from the moment it starts.

### Revoking a leaked credential

Disabling an application and revoking its blob credential are different levers, and confusing
them leaves a leak open. Disabling severs the application's database role — connect and login are
both revoked — which kills its data layer. It does **not** revoke the blob credential: the
container keeps its full access to its own storage container. That independence is the entire
point of a direct credential, and it is also its cost — if a blob credential leaks, disabling the
application does not contain it.

To revoke the blob credential, in order of blast radius:

1. **Delete or back-date the application's own stored access policy** on its storage container —
   the credential is minted against that policy specifically so it can be revoked this way, and
   revocation is immediate. Re-mint a fresh credential (step 5.1) to restore service.
2. **Rotate the storage account key** — the account-wide last resort. It invalidates every
   credential signed with that key, every other application's included, so each live application
   then needs a fresh credential and a redeploy. Use this only when the account key itself is
   suspect.

There is deliberately no rotation lever for the database credential: one role serves both the
sandbox and the deployed container, so resetting its password would cut off a live deployment.
Responding to a leaked database credential means deleting and re-provisioning the project's
database — a separate, larger operator decision, not a step in this procedure.

## 6. Record the handoff

Back in the admin app registry, mark the row deployed — through the console action or the
equivalent admin API call — and include the address the application now runs at. This records
that the deployed pin now matches the approved one, stores the public address, and clears the
"deploy needed" indicator. The address you provide is data, not automation: the platform never
derives, probes or verifies it — it is simply what the owner's own "open your app" link will point
at. Leaving the address out on a re-mark leaves whatever address is already recorded unchanged.

If this action reports a conflict, the row is no longer in a deployable state — most likely a
re-submission or a disable landed while you were deploying. Stop, re-check the row, and if a
re-approval is now pending, re-run this procedure from step 1 for the new pin.

## Known limitations

- There is no automated rollback. To roll an application back, re-run this procedure against an
  earlier approved pin.
- The database credential is minted once and never rotated on its own; its only kill switch is
  disabling the application, or a hard delete, which permanently destroys the application's
  database and role along with every submission bundle. Rotation without a redeploy is not
  supported today.
- Submission bundles are retained without limit until the application itself is deleted.
- Before a first production go-live, confirm the production resource details — the control
  plane's own hosting, the container environment, the cache, the identity provider, and the
  database server — directly against the live environment rather than relying on this document.

## Where to go next

- `reference/openapi.json` — the exact request and response shape of every admin endpoint named
  above.
- `deployment.md` — how the platform's own images are built and where each service runs.
- `redis.md` and `taskiq-worker.md` — the coordination store and background process a deployed
  application does not depend on, but the platform around it does.
